#!/usr/bin/env bash
# =============================================================================
# IDIA Server — AWS Cluster Deploy
# =============================================================================
#
# Deploys the IDIA Server on AWS using the Ray Cluster Launcher (cluster.yaml).
#
# Workflow (decision 2026-06-28 — pre-render approach):
#   1. Load .env and validate required vars
#   2. Pre-render serve_config.yaml (resolve ${VAR} placeholders locally)
#   3. Launch the Ray cluster on EC2 via ray up
#   4. Deploy the LLM application via ray exec
#   5. Print connection info for the dashboard tunnel
#
# Why pre-render instead of env vars on the head node?
#   serve_config.yaml has ${VAR} placeholders filled by render_config.py
#   (Phase 2 design). The Ray Cluster Launcher's file_mounts mechanism
#   copies static files — it doesn't substitute env vars. Pre-rendering
#   locally is simpler and doesn't hardcode secrets in cluster.yaml.
#
# Prerequisites:
#   - ray[default] installed (pip install "ray[default]")
#   - AWS credentials configured (aws configure)
#   - .env file with required variables
#
# Usage:
#   ./scripts/deploy_cluster.sh            # full deploy
#   ./scripts/deploy_cluster.sh --dry-run  # validate .env + pre-render only
#   ./scripts/deploy_cluster.sh --help     # this message
#
# See also:
#   docs/ARCHITECTURE.md §7.3 — Ray Cluster Launcher
#   docs/ARCHITECTURE.md §9 — Security hardening
# =============================================================================

set -euo pipefail

# ── Paths ──────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
CLUSTER_FILE="$REPO_DIR/cluster.yaml"
RENDERED_FILE="$REPO_DIR/rendered_config.yaml"
ENV_FILE="$REPO_DIR/.env"

# ── Parse args ──────────────────────────────────────────────────────────────
# Flags (podem ser combinados):
#   --dry-run   valida + renderiza, sem subir o cluster
#   --spot      usa instâncias Spot no gpu_worker (mais barato, cota separada)
# Também aceita USE_SPOT=true via env var.
DRY_RUN=false
USE_SPOT="${USE_SPOT:-false}"
for arg in "$@"; do
    case "$arg" in
        --help)    sed -n 's/^# //p; s/^#$//p' "$0"; exit 0 ;;
        --dry-run) DRY_RUN=true ;;
        --spot)    USE_SPOT=true ;;
        "")        ;;  # ignora arg vazio (compat com chamada antiga)
        *)         echo "ERROR: flag desconhecida: '$arg' (use --dry-run, --spot, --help)"; exit 1 ;;
    esac
done

# ── Pre-flight checks ───────────────────────────────────────────────────────

if ! command -v ray &> /dev/null; then
    echo "ERROR: 'ray' CLI not found."
    echo "Install with:  pip install 'ray[default]'"
    exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: .env file not found at $ENV_FILE"
    echo "Copy .env.example to .env and fill in your values."
    exit 1
fi

# ── Ensure security group exists (AWS only) ──────────────────────────────────
# Idempotent — safe to re-run. Creates idia-server-sg if it doesn't exist,
# then exports SG_ID for cluster.yaml substitution.
SG_NAME="${SG_NAME:-idia-server-sg}"
if [ -n "$SG_NAME" ] && [ -x "$SCRIPT_DIR/create_security_groups.sh" ]; then
    echo ""
    echo "[Pre] Ensuring security group '$SG_NAME'..."
    SG_OUTPUT=$("$SCRIPT_DIR/create_security_groups.sh" 2>&1 || true)
    echo "$SG_OUTPUT"
    # Extract SG_ID from output if printed by create_security_groups.sh
    EXTRACTED_SG_ID=$(echo "$SG_OUTPUT" | grep -oE 'sg-[a-f0-9]+' | head -1 || true)
    if [ -n "$EXTRACTED_SG_ID" ]; then
        export SG_ID="$EXTRACTED_SG_ID"
        echo "  ✓ SG_ID=$SG_ID"
    fi
fi

# ── Placeholder detection — exact known placeholder values ───────────────────
# These are the verbatim placeholder strings from .env.example.
# Match is EXACT (not substring) to avoid false positives on values
# like "my-secret-key-v2" that happen to contain "change-me".
_is_placeholder() {
    local val="$1"
    case "$val" in
        hf_xxx|sk-litellm-admin-change-me|changeme|your-key-here|TODO|FIXME|placeholder)
            return 0 ;;  # is a placeholder
        *)
            return 1 ;;  # not a placeholder
    esac
}

# ── Load .env ───────────────────────────────────────────────────────────────

# Using set -a to auto-export all variables sourced from .env
set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a

REQUIRED_VARS=("HF_TOKEN" "LITELLM_MASTER_KEY")
MULTI_MODEL=false
if [ -n "${MODELS_COUNT:-}" ] && [ "$MODELS_COUNT" -gt 0 ] 2>/dev/null; then
    MULTI_MODEL=true
    for n in $(seq 1 "$MODELS_COUNT"); do
        REQUIRED_VARS+=("MODEL_${n}_ID" "MODEL_${n}_SOURCE")
    done
else
    REQUIRED_VARS+=("MODEL_ID" "MODEL_SOURCE")
fi

for var in "${REQUIRED_VARS[@]}"; do
    val="${!var:-}"
    if [ -z "$val" ]; then
        echo "ERROR: Required variable '$var' is not set in .env"
        exit 1
    fi
    if _is_placeholder "$val"; then
        echo "ERROR: '$var' appears to be a placeholder value: '$val'"
        echo "       Edit .env and replace it with a real value."
        exit 1
    fi
done

echo "[OK] .env loaded successfully"
if [ "$MULTI_MODEL" = true ]; then
    echo "     MODELS_COUNT=$MODELS_COUNT"
    for n in $(seq 1 "$MODELS_COUNT"); do
        mid_var="MODEL_${n}_ID"
        echo "     ${mid_var}=${!mid_var:-}"
    done
else
    echo "     MODEL_ID=$MODEL_ID"
    echo "     MODEL_SOURCE=<configurado via .env>"
fi

# ── Pre-render config ──────────────────────────────────────────────────────

echo ""
echo "[1/5] Pre-rendering serve_config.yaml..."

if [ ! -f "$SCRIPT_DIR/render_config.py" ]; then
    echo "ERROR: render_config.py not found at $SCRIPT_DIR/render_config.py"
    exit 1
fi

python3 "$SCRIPT_DIR/render_config.py" --dry-run > "$RENDERED_FILE"
echo "  ✓ Created $RENDERED_FILE"

# ── Render cluster.yaml (SG_ID + HF_TOKEN) ──────────────────────────────────
# Ray does NOT expand ${VAR} in the cluster YAML (feature request ray#29016),
# so we substitute the values here BEFORE ray up. Without this, RunInstances
# gets the literal string "${SG_ID}" and fails. rendered_cluster.yaml is
# gitignored — it contains the resolved HF_TOKEN in plaintext.
RENDERED_CLUSTER="$REPO_DIR/rendered_cluster.yaml"

: "${SG_ID:?SG_ID não definido — create_security_groups.sh precisa rodar antes}"
: "${HF_TOKEN:?HF_TOKEN não definido — confira o .env}"

# Bloco Spot: injetado no lugar do marcador ##SPOT_MARKET_OPTIONS## quando
# --spot (ou USE_SPOT=true). Indentação de 6 espaços = nível do node_config.
# Vazio = marcador removido → o worker fica on-demand (padrão).
SPOT_FLAG=0
if [ "$USE_SPOT" = true ]; then
    SPOT_FLAG=1
    echo "  ⚡ Spot habilitado no gpu_worker"
fi

# awk -v não aceita newline embutido no BSD awk (macOS), então passamos só um
# flag numérico e mantemos as linhas do bloco literais no programa.
awk -v spot="$SPOT_FLAG" '
    /##SPOT_MARKET_OPTIONS##/ {
        if (spot == 1) {
            print "      InstanceMarketOptions:"
            print "        MarketType: spot"
        }
        next
    }
    { print }
' "$CLUSTER_FILE" \
  | sed -e "s|\${SG_ID}|${SG_ID}|g" \
        -e "s|\${HF_TOKEN}|${HF_TOKEN}|g" \
  > "$RENDERED_CLUSTER"
echo "  ✓ Rendered cluster config → $RENDERED_CLUSTER"

# ── Dry-run mode ────────────────────────────────────────────────────────────

if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "=== Dry-run mode — cluster launch skipped ==="
    echo "Rendered serve config (→ /app/rendered_config.yaml):"
    echo "--------------------------------------------------------------"
    cat "$RENDERED_FILE"
    echo "--------------------------------------------------------------"
    echo ""
    echo "Rendered cluster config (HF_TOKEN mascarado):"
    echo "--------------------------------------------------------------"
    sed "s|${HF_TOKEN}|<HF_TOKEN>|g" "$RENDERED_CLUSTER"
    echo "--------------------------------------------------------------"
    echo "To launch:  ./scripts/deploy_cluster.sh"
    exit 0
fi

# ── Launch cluster ─────────────────────────────────────────────────────────

echo ""
echo "[2/5] Launching Ray cluster (this takes ~5-10 minutes)..."
echo "      Head node: m5.large (CPU)"
echo "      Worker:    g5.xlarge (GPU) × 0-4 (autoscaled)"

ray up -y "$RENDERED_CLUSTER"

echo "  ✓ Cluster launched"

# ── Deploy LLM app ──────────────────────────────────────────────────────────

echo ""
echo "[3/5] Deploying LLM app..."

ray exec "$RENDERED_CLUSTER" "serve run /app/rendered_config.yaml"

echo "  ✓ LLM app deployed"

# ── Smoke test ──────────────────────────────────────────────────────────────

echo ""
echo "[4/5] Running smoke test..."

HEAD_IP=$(ray get-head-ip "$RENDERED_CLUSTER" 2>/dev/null || true)
if [ -n "$HEAD_IP" ]; then
    if [ -x "$SCRIPT_DIR/smoke_test.sh" ]; then
        "$SCRIPT_DIR/smoke_test.sh" "http://${HEAD_IP}:4000" 2>&1 || {
            echo "  ⚠ Smoke test completed with failures — check logs."
        }
    fi
else
    echo "  ⚠ Could not determine head node IP — skipping smoke test."
    echo "    Run manually: ./scripts/smoke_test.sh http://<head-ip>:4000"
fi

# ── Done ─────────────────────────────────────────────────────────────────────

echo ""
echo "[5/5] Done!"
echo ""
echo "=========================================="
echo " IDIA Server deployed on AWS"
echo "=========================================="
echo ""
echo "Dashboard (SSH tunnel):"
echo "  ray dashboard $RENDERED_CLUSTER"
echo ""
echo "API endpoint (via head node public IP):"
if [ "$MULTI_MODEL" = true ]; then
    # Multi-model: list all model IDs
    echo "  Available models:"
    for n in $(seq 1 "$MODELS_COUNT"); do
        mid_var="MODEL_${n}_ID"
        mid="${!mid_var:-}"
        [ -n "$mid" ] && echo "    • $mid"
    done
    echo ""
    echo "  Example (replace <model> and <head-ip>):"
    FIRST_MODEL_VAR="MODEL_1_ID"
    FIRST_MODEL="${!FIRST_MODEL_VAR:-llama-3.1-8b}"
    echo "  curl -X POST http://<head-public-ip>:4000/chat/completions \\"
    echo "    -H \"Authorization: Bearer \$LITELLM_MASTER_KEY\" \\"
    echo "    -H \"Content-Type: application/json\" \\"
    printf '    -d '"'"'{"model":"%s","messages":[{"role":"user","content":"ping"}]}'"'"'\n' "$FIRST_MODEL"
else
    echo "  curl -X POST http://<head-public-ip>:4000/chat/completions \\"
    echo "    -H \"Authorization: Bearer \$LITELLM_MASTER_KEY\" \\"
    echo "    -H \"Content-Type: application/json\" \\"
    printf '    -d '"'"'{"model":"%s","messages":[{"role":"user","content":"ping"}]}'"'"'\n' "${MODEL_ID:-llama-3.1-8b}"
fi
echo ""
echo "To scale down to zero workers:"
echo "  ray down -y $RENDERED_CLUSTER"
echo ""
echo "=========================================="
