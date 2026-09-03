#!/usr/bin/env bash
# =============================================================================
# scripts/colleague.sh — Provisionamento unificado de usuários
# =============================================================================
#
# Cria virtual key no LiteLLM + usuário no Open WebUI + vincula ambos.
# O admin roda UM comando. O usuário recebe UMA credencial.
#
# Uso:
#   ./scripts/colleague.sh create <email> <nome> [flags]
#   ./scripts/colleague.sh key     <email> [--models M1,M2]   # só key, sem OWUI
#   ./scripts/colleague.sh status  <email>
#   ./scripts/colleague.sh revoke  <email>
#   ./scripts/colleague.sh tiers
#
# Tiers:
#   light      Estagiário / visitante — mínimo
#   regular    Pesquisador — uso diário
#   heavy      Pesquisador sênior — sem limite de req/tok
#   classroom  Sala de aula — 30+ alunos simultâneos
#
# Os modelos concedidos vêm da configuração do servidor (MODEL_ID ou
# MODELS_COUNT/MODEL_N_ID no .env), não de uma lista fixa neste script.
# Use --models para restringir a um subconjunto.
#
# Requer:
#   - .env com LITELLM_MASTER_KEY e IDIA_PUBLIC_HOST
#   - Docker, com o container do Open WebUI no ar
#   - curl, python3, openssl
#
# Compatível com bash 3.2 (macOS) — não usa arrays associativos.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${IDIA_ENV_FILE:-$PROJECT_DIR/.env}"
# Nome exibido em mensagens de uso. O ./idia sobrescreve com "./idia colleague".
PROG="${IDIA_PROG:-$(basename "$0")}"

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly CYAN='\033[0;36m'
readonly BOLD='\033[1m'
readonly RESET='\033[0m'

die()  { echo -e "${RED}ERRO:${RESET} $*" >&2; exit 1; }
warn() { echo -e "${YELLOW}[!] $*${RESET}" >&2; }
info() { echo -e "${CYAN}  $*${RESET}"; }
ok()   { echo -e "${GREEN}  [ok] $*${RESET}"; }

# ═══════════════════════════════════════════════════════════════════════════════
# ENVIRONMENT
# ═══════════════════════════════════════════════════════════════════════════════

_load_env() {
  [ -f "$ENV_FILE" ] || die ".env não encontrado em $ENV_FILE. Rode: cp .env.example .env"
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a

  [ -n "${LITELLM_MASTER_KEY:-}" ] || die "LITELLM_MASTER_KEY não definido em $ENV_FILE"

  # Host anunciado ao usuário nas credenciais. Nunca hardcodado — ver issue #2.
  IDIA_PUBLIC_HOST="${IDIA_PUBLIC_HOST:-localhost}"
  LITELLM_PORT="${LITELLM_PORT:-4000}"
  OWUI_PORT="${OWUI_PORT:-3001}"
  OWUI_CONTAINER="${OWUI_CONTAINER:-idia-webui}"
  LITELLM_URL="http://localhost:${LITELLM_PORT}"
}

# Modelos configurados no servidor, em CSV. Fonte única: o .env.
_configured_models() {
  local count="${MODELS_COUNT:-0}" n mid out=""
  if [ "$count" -gt 0 ] 2>/dev/null; then
    for n in $(seq 1 "$count"); do
      eval "mid=\${MODEL_${n}_ID:-}"
      [ -n "$mid" ] && out="${out}${mid},"
    done
  else
    out="${MODEL_ID:-},"
  fi
  out="${out%,}"
  [ -n "$out" ] || die "Nenhum modelo configurado. Defina MODEL_ID ou MODELS_COUNT no .env."
  echo "$out"
}

# ═══════════════════════════════════════════════════════════════════════════════
# TIERS — case em vez de declare -A, para rodar em bash 3.2 (issue #9)
# ═══════════════════════════════════════════════════════════════════════════════
# Define TIER_BUDGET / TIER_PERIOD / TIER_RPM / TIER_TPM / TIER_DESC.
# RPM/TPM vazios significam "sem limite".

_load_tier() {
  case "$1" in
    light)
      TIER_BUDGET="0.50"; TIER_PERIOD="1d"; TIER_RPM="10";  TIER_TPM="5000"
      TIER_DESC="Visitante / estagiário — \$0.50/dia, 10 req/min, 5K tok/min" ;;
    regular)
      TIER_BUDGET="2";    TIER_PERIOD="1d"; TIER_RPM="60";  TIER_TPM="30000"
      TIER_DESC="Pesquisador, uso diário — \$2/dia, 60 req/min, 30K tok/min" ;;
    heavy)
      TIER_BUDGET="10";   TIER_PERIOD="1d"; TIER_RPM="";    TIER_TPM=""
      TIER_DESC="Pesquisador sênior — \$10/dia, sem limite de req/tok" ;;
    classroom)
      TIER_BUDGET="20";   TIER_PERIOD="1d"; TIER_RPM="300"; TIER_TPM="200000"
      TIER_DESC="Sala de aula, 30+ alunos — \$20/dia, 300 req/min, 200K tok/min" ;;
    *)
      return 1 ;;
  esac
}

# ═══════════════════════════════════════════════════════════════════════════════
# LiteLLM
# ═══════════════════════════════════════════════════════════════════════════════

_litellm_api() {
  local method="$1" path="$2" data="${3:-}"
  if [ -n "$data" ]; then
    curl -s --max-time 10 -X "$method" "${LITELLM_URL}${path}" \
      -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
      -H "Content-Type: application/json" \
      -d "$data"
  else
    curl -s --max-time 10 -X "$method" "${LITELLM_URL}${path}" \
      -H "Authorization: Bearer ${LITELLM_MASTER_KEY}"
  fi
}

# Apaga TODAS as keys com um dado alias. Imprime a contagem.
# A master key vai pelo ambiente; o alias por argv. Nada é interpolado
# no fonte Python — ver issue #4.
_litellm_delete_keys_by_alias() {
  LITELLM_URL="$LITELLM_URL" LITELLM_MASTER_KEY="$LITELLM_MASTER_KEY" \
  python3 - "$1" <<'PY'
import json, os, sys, urllib.error, urllib.request

alias = sys.argv[1]
base = os.environ["LITELLM_URL"]
master = os.environ["LITELLM_MASTER_KEY"]


def api(path, method="GET", payload=None):
    req = urllib.request.Request(base + path, method=method)
    req.add_header("Authorization", f"Bearer {master}")
    req.add_header("Content-Type", "application/json")
    body = json.dumps(payload).encode() if payload else None
    try:
        with urllib.request.urlopen(req, data=body, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


deleted = 0
for token in api("/key/list").get("keys", []):
    info = api(f"/key/info?key={token}")
    if info.get("info", {}).get("key_alias") == alias:
        api("/key/delete", method="POST", payload={"keys": [token]})
        deleted += 1
print(deleted)
PY
}

# ═══════════════════════════════════════════════════════════════════════════════
# Open WebUI — SQLite direto (ver ADR-012 para o porquê)
# ═══════════════════════════════════════════════════════════════════════════════
# O script Python chega pelo stdin (heredoc com delimitador entre aspas: bash
# não expande nada dentro dele). Os valores chegam por argv. Essa separação é
# o que impede que um nome com apóstrofo vire código — ver issue #4.
#
# Nota: argv é visível em `ps` na máquina host. Aceitável aqui (o script roda
# como admin num servidor de acesso restrito) e estritamente melhor do que
# interpolar no fonte. Se isso deixar de valer, mover para stdin em JSON.

_owui_py() {
  docker exec -i "$OWUI_CONTAINER" python3 - "$@"
}

_owui_check() {
  docker inspect "$OWUI_CONTAINER" >/dev/null 2>&1 \
    || die "Container '${OWUI_CONTAINER}' não encontrado. Suba o Open WebUI ou defina OWUI_CONTAINER."
}

# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: create
# ═══════════════════════════════════════════════════════════════════════════════

cmd_create() {
  [ $# -ge 2 ] || die "Uso: $PROG create <email> <nome> [flags]\n  Veja: $PROG --help"

  local EMAIL="$1"; shift
  local NAME="$1"; shift

  local TIER="regular" MODELS="" BUDGET="" BUDGET_PERIOD="" RPM="" TPM=""
  local MAX_PARALLEL="" EXPIRES="" PASSWORD="" ROLE="user" TAG=""
  local BLOCKED="false" NO_OPENWEBUI="false" DRY_RUN="false"

  while [ $# -gt 0 ]; do
    case "$1" in
      --tier)          TIER="$2"; shift 2 ;;
      --models)        MODELS="$2"; shift 2 ;;
      --budget)        BUDGET="$2"; shift 2 ;;
      --budget-period) BUDGET_PERIOD="$2"; shift 2 ;;
      --rpm)           RPM="$2"; shift 2 ;;
      --tpm)           TPM="$2"; shift 2 ;;
      --max-parallel)  MAX_PARALLEL="$2"; shift 2 ;;
      --expires)       EXPIRES="$2"; shift 2 ;;
      --password)      PASSWORD="$2"; shift 2 ;;
      --role)          ROLE="$2"; shift 2 ;;
      --tag)           TAG="$2"; shift 2 ;;
      --blocked)       BLOCKED="true"; shift ;;
      --no-openwebui)  NO_OPENWEBUI="true"; shift ;;
      --dry-run)       DRY_RUN="true"; shift ;;
      *) die "Flag desconhecida: $1\n  Veja: $PROG --help" ;;
    esac
  done

  _load_tier "$TIER" || die "Tier inválido: '${TIER}'. Use: light, regular, heavy, classroom"

  [ -n "$BUDGET" ]        || BUDGET="$TIER_BUDGET"
  [ -n "$BUDGET_PERIOD" ] || BUDGET_PERIOD="$TIER_PERIOD"
  [ -n "$RPM" ]           || RPM="$TIER_RPM"
  [ -n "$TPM" ]           || TPM="$TIER_TPM"
  [ -n "$MODELS" ]        || MODELS="$(_configured_models)"
  [ -n "$PASSWORD" ]      || PASSWORD="$(openssl rand -base64 9 2>/dev/null \
                                          || python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"

  local ALIAS="${EMAIL%%@*}"

  echo ""
  echo -e "${BOLD}=== Provisionando: ${EMAIL} ===${RESET}"
  echo ""
  echo -e "  Tier:       ${CYAN}${TIER}${RESET} — ${TIER_DESC}"
  echo -e "  Nome:       ${NAME}"
  echo -e "  Role OWUI:  ${ROLE}"
  echo -e "  Models:     ${MODELS}"
  echo -e "  Budget:     \$${BUDGET} / ${BUDGET_PERIOD}"
  echo -e "  RPM limit:  ${RPM:-ilimitado}"
  echo -e "  TPM limit:  ${TPM:-ilimitado}"
  [ -n "$MAX_PARALLEL" ]  && echo -e "  Max paral:  ${MAX_PARALLEL}"
  [ -n "$EXPIRES" ]       && echo -e "  Expires:    ${EXPIRES}"
  [ "$BLOCKED" = "true" ] && echo -e "  ${YELLOW}Blocked:    sim (precisa ativar depois)${RESET}"
  [ -n "$TAG" ]           && echo -e "  Tag:        ${TAG}"
  echo ""

  if [ "$DRY_RUN" = "true" ]; then
    echo -e "${YELLOW}  DRY RUN — nada foi criado. Remova --dry-run para executar.${RESET}"
    return 0
  fi

  [ "$NO_OPENWEBUI" = "true" ] || _owui_check

  # ── [1/6] Limpeza ────────────────────────────────────────────────────────
  info "[1/6] Limpando chaves anteriores (alias: ${ALIAS})..."
  _litellm_delete_keys_by_alias "$ALIAS" >/dev/null
  ok "Alias '${ALIAS}' limpo"

  # ── [2/6] Virtual key ────────────────────────────────────────────────────
  info "[2/6] Criando virtual key no LiteLLM..."

  local payload
  payload=$(python3 - "$ALIAS" "$TIER" "$MODELS" "$BUDGET" "$BUDGET_PERIOD" \
                     "$RPM" "$TPM" "$MAX_PARALLEL" "$EXPIRES" "$TAG" "$BLOCKED" <<'PY'
import json, sys

(alias, tier, models, budget, period,
 rpm, tpm, max_parallel, expires, tag, blocked) = sys.argv[1:12]

p = {
    "key_alias": alias,
    "team_id": tier,
    "models": [m.strip() for m in models.split(",") if m.strip()],
    "max_budget": float(budget),
    "budget_duration": period,
}
if rpm:          p["rpm_limit"] = int(rpm)
if tpm:          p["tpm_limit"] = int(tpm)
if max_parallel: p["max_parallel_requests"] = int(max_parallel)
if expires:      p["expires"] = expires
if tag:          p["tags"] = [tag]
if blocked == "true": p["blocked"] = True
print(json.dumps(p))
PY
)

  local key_response key
  key_response=$(_litellm_api POST /key/generate "$payload")
  key=$(printf '%s' "$key_response" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("key",""))' 2>/dev/null || echo "")

  case "$key" in
    sk-*) ok "Key: ${key:0:25}..." ;;
    *)    die "LiteLLM não devolveu uma chave válida.\n  Resposta: ${key_response:0:300}" ;;
  esac

  if [ "$NO_OPENWEBUI" = "true" ]; then
    info "[3/6] Pulando Open WebUI (--no-openwebui)"
    echo ""
    echo -e "${BOLD}=== API KEY (sem conta Open WebUI) ===${RESET}"
    echo -e "  Key:     ${key}"
    echo -e "  API:     http://${IDIA_PUBLIC_HOST}:${LITELLM_PORT}/v1"
    echo -e "  Models:  ${MODELS}"
    echo ""
    return 0
  fi

  # ── [3/6] Usuário no Open WebUI ──────────────────────────────────────────
  info "[3/6] Criando usuário no Open WebUI..."

  local user_id
  user_id=$(_owui_py "$EMAIL" "$NAME" "$ROLE" "$PASSWORD" <<'PY'
import sqlite3, sys, time, uuid
import bcrypt

email, name, role, password = sys.argv[1:5]
conn = sqlite3.connect("/app/backend/data/webui.db")
now = int(time.time())
hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

row = conn.execute('SELECT id FROM "user" WHERE email = ?', (email,)).fetchone()
if row:
    uid = row[0]
    conn.execute("UPDATE auth SET password = ? WHERE id = ?", (hashed, uid))
else:
    uid = str(uuid.uuid4())
    conn.execute(
        'INSERT INTO "user" (id, name, email, role, created_at, updated_at, last_active_at)'
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (uid, name, email, role, now, now, now),
    )
    conn.execute(
        "INSERT INTO auth (id, email, password, active) VALUES (?, ?, ?, ?)",
        (uid, email, hashed, 1),
    )
conn.commit()
conn.close()
print(uid)
PY
) || die "Falha ao criar/atualizar o usuário no Open WebUI"

  [ -n "$user_id" ] || die "Open WebUI não devolveu um id de usuário"
  ok "Usuário pronto (role: ${ROLE}, id: ${user_id:0:8}...)"

  # ── [4/6] Vincular chave ─────────────────────────────────────────────────
  info "[4/6] Vinculando chave ao usuário..."
  _owui_py "$user_id" "$key" <<'PY' >/dev/null || die "Falha ao vincular a chave ao usuário"
import sqlite3, sys, time

user_id, key_val = sys.argv[1:3]
conn = sqlite3.connect("/app/backend/data/webui.db")
now = int(time.time() * 1000)

conn.execute("DELETE FROM api_key WHERE user_id = ?", (user_id,))
conn.execute(
    "INSERT INTO api_key (id, user_id, key, data, created_at, updated_at)"
    " VALUES (?, ?, ?, ?, ?, ?)",
    ("key_" + user_id, user_id, key_val, "{}", now, now),
)
conn.commit()
conn.close()
PY
  ok "Chave vinculada"

  # ── [5/6] Visibilidade de modelos ────────────────────────────────────────
  # Padrão override + access grants — ver ADR-012.
  info "[5/6] Configurando visibilidade de modelos..."
  _owui_py "$user_id" "$MODELS" "$(_configured_models)" <<'PY' >/dev/null \
    || die "Falha ao configurar a visibilidade de modelos"
import json, sqlite3, sys, time, uuid

user_id, granted_csv, all_csv = sys.argv[1:4]
granted = [m.strip() for m in granted_csv.split(",") if m.strip()]
all_models = [m.strip() for m in all_csv.split(",") if m.strip()]

conn = sqlite3.connect("/app/backend/data/webui.db")
cur = conn.cursor()
now = int(time.time())

row = cur.execute(
    'SELECT id FROM "user" WHERE role = "admin" ORDER BY created_at LIMIT 1'
).fetchone()
admin_id = row[0] if row else user_id

meta = json.dumps(
    {"profile_image_url": "", "description": "", "capabilities": {}, "visibility": "private"}
)

# base_model_id NULL ativa o branch "override" do get_all_models() (ADR-012).
for mid in all_models:
    cur.execute(
        "INSERT OR IGNORE INTO model"
        " (id, user_id, base_model_id, name, params, meta, updated_at, created_at, is_active)"
        ' VALUES (?, ?, NULL, ?, "{}", ?, ?, ?, 1)',
        (mid, admin_id, mid, meta, now, now),
    )
    cur.execute(
        "UPDATE model SET user_id = ?, base_model_id = NULL, updated_at = ?"
        " WHERE id = ? AND (base_model_id IS NOT NULL OR user_id IS NULL)",
        (admin_id, now, mid),
    )

cur.execute(
    "DELETE FROM access_grant WHERE resource_type = 'model'"
    " AND principal_type = 'user' AND principal_id = ?",
    (user_id,),
)
for mid in granted:
    cur.execute(
        "INSERT INTO access_grant"
        " (id, resource_type, resource_id, principal_type, principal_id, permission, created_at)"
        " VALUES (?, 'model', ?, 'user', ?, 'read', ?)",
        (str(uuid.uuid4()), mid, user_id, now),
    )

conn.commit()
conn.close()
PY
  ok "Visibilidade configurada (${MODELS})"

  # ── [6/6] Config global do dropdown ──────────────────────────────────────
  info "[6/6] Garantindo config global (dropdown de modelos)..."
  if [ -z "${OWUI_DISCOVERY_KEY:-}" ]; then
    warn "OWUI_DISCOVERY_KEY não definida — pulando a config global do dropdown."
    warn "Defina-a no .env com uma virtual key dedicada à descoberta de modelos."
  else
    _owui_py "http://litellm:${LITELLM_PORT}/v1" "$OWUI_DISCOVERY_KEY" <<'PY' >/dev/null \
      || die "Falha ao gravar a config global do Open WebUI"
import json, sqlite3, sys, time

api_base, discovery_key = sys.argv[1:3]
conn = sqlite3.connect("/app/backend/data/webui.db")
now = int(time.time())

for k, v in (
    ("openai.enable", "true"),
    ("openai.api_base_urls", json.dumps([api_base])),
    ("openai.api_keys", json.dumps([discovery_key])),
):
    conn.execute(
        "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?",
        (k, v, now, v, now),
    )
conn.commit()
conn.close()
PY
    ok "Config global garantida"
  fi

  # ── Resultado ────────────────────────────────────────────────────────────
  echo ""
  echo -e "${BOLD}==========================================================${RESET}"
  echo -e "${BOLD}  CREDENCIAIS — Entregar ao usuário${RESET}"
  echo -e "${BOLD}==========================================================${RESET}"
  echo ""
  echo -e "  ${CYAN}Acesse:${RESET}  http://${IDIA_PUBLIC_HOST}:${OWUI_PORT}"
  echo -e "  ${CYAN}Email:${RESET}   ${EMAIL}"
  echo -e "  ${CYAN}Senha:${RESET}   ${PASSWORD}"
  echo -e "  ${CYAN}Tier:${RESET}    ${TIER}"
  echo -e "  ${CYAN}Models:${RESET}  ${MODELS}"
  echo ""
  echo -e "  ${YELLOW}(Troque a senha no primeiro login)${RESET}"
  echo -e "${BOLD}==========================================================${RESET}"
  echo ""
}

# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: key — virtual key apenas
# ═══════════════════════════════════════════════════════════════════════════════

cmd_key() {
  [ $# -ge 1 ] || die "Uso: $PROG key <email> [--models M1,M2] [--tier TIER]"
  cmd_create "$1" "${1%%@*}" --no-openwebui "${@:2}"
}

# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: status
# ═══════════════════════════════════════════════════════════════════════════════

cmd_status() {
  [ $# -ge 1 ] || die "Uso: $PROG status <email>"
  local EMAIL="$1" ALIAS="${1%%@*}"

  echo -e "${BOLD}=== Status: ${EMAIL} ===${RESET}"
  echo ""
  echo -e "${CYAN}Open WebUI:${RESET}"

  if docker inspect "$OWUI_CONTAINER" >/dev/null 2>&1; then
    _owui_py "$EMAIL" <<'PY' || warn "  falha ao consultar o Open WebUI"
import sqlite3, sys

email = sys.argv[1]
conn = sqlite3.connect("/app/backend/data/webui.db")
row = conn.execute('SELECT id, name, role FROM "user" WHERE email = ?', (email,)).fetchone()
if not row:
    print("  usuário não encontrado")
else:
    uid, name, role = row
    print(f"  Nome: {name}")
    print(f"  Role: {role}")
    kr = conn.execute("SELECT key FROM api_key WHERE user_id = ?", (uid,)).fetchone()
    print(f"  Key:  {kr[0][:25]}..." if kr else "  Key:  (não configurada)")
    grants = conn.execute(
        "SELECT resource_id FROM access_grant"
        " WHERE resource_type = 'model' AND principal_type = 'user' AND principal_id = ?",
        (uid,),
    ).fetchall()
    print(f"  Models: {', '.join(g[0] for g in grants) or '(nenhum)'}")
conn.close()
PY
  else
    warn "  container '${OWUI_CONTAINER}' não está no ar"
  fi

  echo ""
  echo -e "${CYAN}LiteLLM:${RESET}"
  LITELLM_URL="$LITELLM_URL" LITELLM_MASTER_KEY="$LITELLM_MASTER_KEY" \
  python3 - "$ALIAS" <<'PY' || warn "  LiteLLM indisponível"
import json, os, sys, urllib.request

alias = sys.argv[1]
url = f"{os.environ['LITELLM_URL']}/key/info?key_alias={alias}"
req = urllib.request.Request(url)
req.add_header("Authorization", f"Bearer {os.environ['LITELLM_MASTER_KEY']}")
with urllib.request.urlopen(req, timeout=5) as r:
    keys = json.loads(r.read()).get("data", {}).get("keys", [])

if not keys:
    print("  (nenhuma key encontrada)")
else:
    k = keys[0]
    print(f"  Alias:   {k.get('key_alias', '?')}")
    print(f"  Spend:   ${k.get('spend', 0):.4f}")
    print(f"  Budget:  ${k.get('max_budget', 0)} / {k.get('budget_duration', '?')}")
    print(f"  RPM:     {k.get('rpm_limit') or 'ilimitado'}")
    print(f"  TPM:     {k.get('tpm_limit') or 'ilimitado'}")
    print(f"  Models:  {k.get('models', [])}")
PY
}

# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: revoke
# ═══════════════════════════════════════════════════════════════════════════════

cmd_revoke() {
  [ $# -ge 1 ] || die "Uso: $PROG revoke <email>"
  local EMAIL="$1" ALIAS="${1%%@*}"

  echo -e "${BOLD}=== Revogando: ${EMAIL} ===${RESET}"

  local deleted
  deleted=$(_litellm_delete_keys_by_alias "$ALIAS")
  if [ "${deleted:-0}" -gt 0 ] 2>/dev/null; then
    ok "Key(s) LiteLLM revogada(s): ${deleted}"
  else
    info "Nenhuma key LiteLLM para '${ALIAS}'"
  fi

  _owui_check
  _owui_py "$EMAIL" <<'PY' || die "Falha ao remover o usuário do Open WebUI"
import sqlite3, sys

email = sys.argv[1]
conn = sqlite3.connect("/app/backend/data/webui.db")
row = conn.execute('SELECT id FROM "user" WHERE email = ?', (email,)).fetchone()
if not row:
    print("NOT_FOUND")
else:
    uid = row[0]
    conn.execute("DELETE FROM api_key WHERE user_id = ?", (uid,))
    conn.execute(
        "DELETE FROM access_grant WHERE principal_type = 'user' AND principal_id = ?", (uid,)
    )
    conn.execute("DELETE FROM auth WHERE id = ?", (uid,))
    conn.execute('DELETE FROM "user" WHERE id = ?', (uid,))
    conn.commit()
    print("DELETED")
conn.close()
PY
  ok "Open WebUI limpo"
}

# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: tiers
# ═══════════════════════════════════════════════════════════════════════════════

cmd_tiers() {
  local models
  models="$(_configured_models)"
  echo -e "${BOLD}=== TIERS DISPONÍVEIS ===${RESET}"
  echo ""
  local t
  for t in light regular heavy classroom; do
    _load_tier "$t"
    echo -e "  ${CYAN}${t}${RESET}"
    echo -e "    Budget:  \$${TIER_BUDGET} / ${TIER_PERIOD}"
    echo -e "    RPM:     ${TIER_RPM:-ilimitado}"
    echo -e "    TPM:     ${TIER_TPM:-ilimitado}"
    echo -e "    Models:  ${models}   (todos os configurados; restrinja com --models)"
    echo -e "    ${TIER_DESC}"
    echo ""
  done
}

# ═══════════════════════════════════════════════════════════════════════════════
# HELP
# ═══════════════════════════════════════════════════════════════════════════════

cmd_help() {
  cat <<EOF
IDIA Colleague Manager

Uso: $PROG {create|key|status|revoke|tiers} [args...]

create <email> <nome> [flags]
  Cria virtual key + usuário Open WebUI + vincula ambos.

  Flags:
    --tier TIER          light|regular|heavy|classroom (default: regular)
    --models M1,M2,...   Restringe os modelos (default: todos os configurados)
    --budget FLOAT       Orçamento máximo USD (default: por tier)
    --budget-period PER  1d|7d|1mo|30d (default: 1d)
    --rpm INT            Requests/minuto
    --tpm INT            Tokens/minuto
    --max-parallel INT   Requisições simultâneas
    --expires DATE       Expiração YYYY-MM-DD
    --password STR       Senha customizada (default: auto-gerada)
    --role ROLE          user|admin (default: user)
    --tag STR            Tag para organização
    --blocked            Criar key bloqueada
    --no-openwebui       Apenas key, sem conta OWUI
    --dry-run            Mostrar o plano sem criar nada

key <email> [flags]      Só a virtual key (equivale a create --no-openwebui)
status <email>           Estado no Open WebUI e no LiteLLM
revoke <email>           Remove key, grants, api_key, auth e usuário
tiers                    Definições de tier e modelos concedidos

Variáveis de ambiente (via .env):
  LITELLM_MASTER_KEY   obrigatória
  IDIA_PUBLIC_HOST     host anunciado nas credenciais (default: localhost)
  OWUI_DISCOVERY_KEY   virtual key usada só para descoberta de modelos
  OWUI_CONTAINER       nome do container Open WebUI (default: idia-webui)
  LITELLM_PORT         default 4000
  OWUI_PORT            default 3001

Exemplos:
  $PROG create ana@idia.org "Ana Costa" --tier heavy --budget 50 --budget-period 1mo
  $PROG create turma@idia.org "Sala 202" --tier classroom
  $PROG create bot@idia.org "Bot" --no-openwebui --tier light
  $PROG tiers
  $PROG status ana@idia.org
  $PROG revoke ana@idia.org
EOF
}

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

case "${1:-}" in
  --help|-h|help|"") cmd_help ;;
  create)  _load_env; shift; cmd_create "$@" ;;
  key)     _load_env; shift; cmd_key    "$@" ;;
  status)  _load_env; shift; cmd_status "$@" ;;
  revoke)  _load_env; shift; cmd_revoke "$@" ;;
  tiers)   _load_env; cmd_tiers ;;
  *) die "Comando desconhecido: '${1}'\n  Use: create, key, status, revoke, tiers, --help" ;;
esac
