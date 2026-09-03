#!/usr/bin/env bash
# =============================================================================
# scripts/gate.sh — o portão local, rodado antes de abrir um PR
# =============================================================================
#
# Uso:
#   ./scripts/gate.sh
#
# Roda tudo que dá para rodar sem GPU, sem Docker e sem servidor no ar:
# a suíte pytest, a sintaxe dos scripts shell, e o lint dos arquivos Python.
#
# Existe para que "CI descobre" vire "CI confirma". Um PR aberto sem passar
# por aqui transforma a esteira em ferramenta de descoberta e dobra o custo
# de cada fatia.
#
# O caminho é declarado em `.claude/portao`, que o git-guard lê para recusar
# `gh pr create` sem gate recente.
# =============================================================================

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

if [ -t 1 ] && command -v tput &>/dev/null; then
    BOLD="$(tput bold)"; GREEN="$(tput setaf 2)"; RED="$(tput setaf 1)"
    YELLOW="$(tput setaf 3)"; RESET="$(tput sgr0)"
else
    BOLD="" GREEN="" RED="" YELLOW="" RESET=""
fi

_step() { echo ""; echo "${BOLD}── $* ──${RESET}"; }
_ok()   { echo "${GREEN}[ok]${RESET} $*"; }
_fail() { echo "${RED}[falhou]${RESET} $*" >&2; exit 1; }
_skip() { echo "${YELLOW}[pulado]${RESET} $*"; }

# ── 1. Suíte de testes ──────────────────────────────────────────────────────

_step "pytest"
python3 -m pytest -q || _fail "a suíte não passou"
_ok "suíte verde"

# ── 2. Sintaxe dos scripts shell ────────────────────────────────────────────

_step "sintaxe shell"
shell_files=(idia)
while IFS= read -r f; do shell_files+=("$f"); done < <(find scripts -name '*.sh' -type f | sort)
for f in "${shell_files[@]}"; do
    bash -n "$f" || _fail "erro de sintaxe em $f"
done
_ok "${#shell_files[@]} script(s) sem erro de sintaxe"

# Opcional: nem toda máquina tem o shellcheck instalado.
# (Não começar este comentário com o nome da ferramenta — ela lê a linha
#  como diretiva e falha ao parseá-la.)
if command -v shellcheck &>/dev/null; then
    shellcheck -S error "${shell_files[@]}" || _fail "shellcheck apontou erros"
    _ok "shellcheck limpo"
else
    _skip "shellcheck não instalado (brew install shellcheck)"
fi

# ── 3. Lint Python ──────────────────────────────────────────────────────────
# Só os arquivos que este projeto mantém limpos. A base tem achados antigos
# em tests/ que não vale corrigir de passagem — corrigi-los alarga todo diff
# que passar por perto.

_step "ruff"
if python3 -m ruff --version &>/dev/null; then
    python3 -m ruff check scripts/render_config.py \
        tests/test_colleague.py \
        tests/test_engine_config.py \
        tests/test_stack_services.py \
        || _fail "ruff apontou erros"
    _ok "ruff limpo nos arquivos mantidos"
else
    _skip "ruff não instalado (pip install ruff)"
fi

# ── 4. Configs parseiam ─────────────────────────────────────────────────────

_step "configs"
python3 - <<'PY' || exit 1
import sys
import yaml

for path in ("docker-compose.yml", "serve_config.yaml", "prometheus.yml"):
    try:
        with open(path, encoding="utf-8") as fh:
            yaml.safe_load(fh)
    except Exception as exc:                      # noqa: BLE001
        print(f"[falhou] {path}: {exc}", file=sys.stderr)
        sys.exit(1)
print("  3 arquivos de configuração parseiam")
PY
_ok "configs válidos"

# ── 5. Marca o portão ───────────────────────────────────────────────────────
# Sem isto o git-guard recusa o `gh pr create` que este gate acabou de aprovar.

if [ -x "$HOME/.claude/hooks/git-guard.sh" ]; then
    "$HOME/.claude/hooks/git-guard.sh" --stamp || true
fi

echo ""
echo "${BOLD}${GREEN}Portão passou.${RESET} Pode abrir o PR."
echo ""
