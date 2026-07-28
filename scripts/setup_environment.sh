#!/usr/bin/env bash
# =============================================================================
# setup_environment.sh — Prepara o ambiente para o IDIA Server
# =============================================================================
#
# Idempotente — seguro rodar múltiplas vezes.
# Cada passo verifica se já está ok antes de agir.
# =============================================================================

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPTS_DIR/.." && pwd)"

# ── Colours ──────────────────────────────────────────────────────────────────

if [ -t 1 ] && command -v tput &>/dev/null; then
    GREEN="$(tput setaf 2)"
    YELLOW="$(tput setaf 3)"
    RED="$(tput setaf 1)"
    BOLD="$(tput bold)"
    RESET="$(tput sgr0)"
else
    GREEN="" YELLOW="" RED="" BOLD="" RESET=""
fi

_info()  { echo "${GREEN}[✓]${RESET} $*"; }
_warn()  { echo "${YELLOW}[!]${RESET} $*"; }
_error() { echo "${RED}[✗]${RESET} $*" >&2; }
_step()  { echo ""; echo "${BOLD}[$1]${RESET} $2"; }

echo ""
echo "${BOLD}${GREEN}══════════════════════════════════════${RESET}"
echo "${BOLD}${GREEN}  IDIA Server — Environment Setup${RESET}"
echo "${BOLD}${GREEN}══════════════════════════════════════${RESET}"
echo ""

# ── Step 0: Pre-flight ──────────────────────────────────────────────────────

_step "0/5" "Checking prerequisites..."

FAIL=0

if [ "$(uname -s)" != "Linux" ]; then
    _error "This script only supports Linux."
    exit 1
fi
_info "OS: $(uname -s)"

if ! command -v nvidia-smi &>/dev/null || ! nvidia-smi &>/dev/null; then
    _warn "No NVIDIA GPU detected — inference won't work, but setup continues"
else
    GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l)
    _info "GPU: ${GPU_COUNT}x $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
fi

if ! command -v docker &>/dev/null; then
    _error "Docker not found. Install Docker first:  sudo yum install -y docker"
    FAIL=1
else
    _info "Docker: $(docker --version 2>&1 | head -1)"
fi

if ! command -v nvidia-ctk &>/dev/null; then
    _warn "NVIDIA Container Toolkit not found — GPU passthrough may not work"
else
    _info "NVIDIA Container Toolkit: $(nvidia-ctk --version 2>&1 | head -1)"
fi

if [ $FAIL -eq 1 ]; then exit 1; fi

# ── Step 1: Docker Compose v2 ────────────────────────────────────────────────

_step "1/5" "Docker Compose v2..."

if docker compose version &>/dev/null 2>&1; then
    _info "Already installed: $(docker compose version --short 2>/dev/null || echo 'v2.x')"
else
    echo "       Installing Docker Compose plugin..."
    COMPOSE_VER="v2.27.0"
    COMPOSE_URL="https://github.com/docker/compose/releases/download/${COMPOSE_VER}/docker-compose-$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)"

    sudo mkdir -p /usr/local/lib/docker/cli-plugins
    sudo curl -SfL --progress-bar "$COMPOSE_URL" -o /usr/local/lib/docker/cli-plugins/docker-compose
    sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

    # Verify the downloaded file is a real binary (not an error page)
    FSIZE=$(stat -c%s /usr/local/lib/docker/cli-plugins/docker-compose 2>/dev/null || echo "0")
    if [ "$FSIZE" -lt 1000000 ]; then
        _error "Download failed (file size: ${FSIZE} bytes). URL: ${COMPOSE_URL}"
        sudo rm -f /usr/local/lib/docker/cli-plugins/docker-compose
        exit 1
    fi

    if docker compose version &>/dev/null; then
        _info "Installed: $(docker compose version --short 2>/dev/null || echo 'v2.x')"
    else
        _error "Installation failed. Check: ls -la /usr/local/lib/docker/cli-plugins/"
        exit 1
    fi
fi

# ── Step 2: Python 3.12 ──────────────────────────────────────────────────────

_step "2/5" "Python 3.12 as default..."

PYTHON_VER=$(python3 --version 2>&1 | grep -oP '\d+\.\d+' || echo "0")
if [ "$PYTHON_VER" = "3.12" ] || [ "$PYTHON_VER" = "3.13" ]; then
    _info "Python $PYTHON_VER already default"
else
    if [ -f /usr/bin/python3.12 ]; then
        echo "       Current python3 → Python ${PYTHON_VER}. Switching to 3.12..."
        sudo alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 100 2>/dev/null || true
        sudo alternatives --set python3 /usr/bin/python3.12 2>/dev/null || true

        NEW_VER=$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
        if [ "$NEW_VER" = "3.12" ] || [ "$NEW_VER" = "3.13" ]; then
            _info "Python $NEW_VER set as default"
        else
            _error "Failed to set python3.12 as default."
            exit 1
        fi
    else
        _error "Python 3.12 not found at /usr/bin/python3.12. Install it first."
        exit 1
    fi
fi

# ── Step 3: pyyaml ───────────────────────────────────────────────────────────

_step "3/5" "pyyaml..."

if python3 -c "import yaml; v=yaml.__version__; assert v >= '6', f'got {v}'" 2>/dev/null; then
    _info "Already installed: $(python3 -c 'import yaml; print(yaml.__version__)')"
else
    echo "       Installing pyyaml..."
    pip3 install --user --quiet "pyyaml>=6.0,<7.0" 2>&1
    if python3 -c "import yaml" 2>/dev/null; then
        _info "Installed: $(python3 -c 'import yaml; print(yaml.__version__)')"
    else
        _error "pyyaml installation failed. Try: pip3 install --user 'pyyaml>=6.0,<7.0'"
        exit 1
    fi
fi

# ── Step 4: .env ─────────────────────────────────────────────────────────────

_step "4/5" ".env configuration..."

if [ -f "$REPO_DIR/.env" ]; then
    _info ".env already exists — preserved"
else
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
    _info ".env created from .env.example"
    echo ""
    echo "       ${YELLOW}╔══════════════════════════════════════════════════════╗${RESET}"
    echo "       ${YELLOW}║  EDIT .env BEFORE DEPLOYING                          ║${RESET}"
    echo "       ${YELLOW}║                                                      ║${RESET}"
    echo "       ${YELLOW}║  nano $REPO_DIR/.env${RESET}"
    echo "       ${YELLOW}║                                                      ║${RESET}"
    echo "       ${YELLOW}║  Required: HF_TOKEN, LITELLM_MASTER_KEY,             ║${RESET}"
    echo "       ${YELLOW}║            MODEL_ID, MODEL_SOURCE                     ║${RESET}"
    echo "       ${YELLOW}╚══════════════════════════════════════════════════════╝${RESET}"
    echo ""
fi

# ── Step 5: Verify ───────────────────────────────────────────────────────────

_step "5/5" "Verifying environment..."

PASS=0
FAILED=""

docker compose version &>/dev/null 2>&1 && PASS=$((PASS+1)) || FAILED="$FAILED docker-compose"
python3 --version 2>&1 | grep -qE "3\.1[2-9]" && PASS=$((PASS+1)) || FAILED="$FAILED python3"
python3 -c "import yaml" 2>/dev/null && PASS=$((PASS+1)) || FAILED="$FAILED pyyaml"
[ -f "$REPO_DIR/.env" ] && PASS=$((PASS+1)) || FAILED="$FAILED .env"
bash "$REPO_DIR/idia" --help &>/dev/null 2>&1 && PASS=$((PASS+1)) || FAILED="$FAILED idia"

echo ""
if [ $PASS -eq 5 ]; then
    _info "All checks passed (5/5)"
else
    _warn "Some checks failed ($PASS/5):${FAILED}"
fi

echo ""
echo "${BOLD}${GREEN}══════════════════════════════════════${RESET}"
echo "${BOLD}${GREEN}  Setup complete${RESET}"
echo "${BOLD}${GREEN}══════════════════════════════════════${RESET}"
echo ""
echo "${BOLD}Next steps:${RESET}"
echo "  1. ${GREEN}nano .env${RESET}                         # Fill in HF_TOKEN + model config"
echo "  2. ${GREEN}./idia deploy local --dry-run${RESET}     # Validate config"
echo "  3. ${GREEN}./idia deploy local${RESET}              # Start the server"
echo "  4. ${GREEN}sudo ./idia service install${RESET}      # Auto-start on boot"
echo ""
