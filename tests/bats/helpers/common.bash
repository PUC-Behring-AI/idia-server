#!/usr/bin/env bash
# Shared setup for the bats suite.
#
# Every test gets: a throwaway .env, a fake LiteLLM listening on an ephemeral
# port, and tests/bats/helpers/bin first on PATH so `docker` is the stub.
#
# Nothing here touches the operator's real .env or a real container: the
# scripts under test accept IDIA_ENV_FILE, and the .env we write points
# LITELLM_PORT at the fake server.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
export REPO_ROOT
HELPERS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export HELPERS_DIR

# ── PATH stubs ───────────────────────────────────────────────────────────────

stub_path() {
    PATH="${HELPERS_DIR}/bin:${PATH}"
    export PATH
    FAKE_DOCKER_LOG="${BATS_TEST_TMPDIR}/docker.log"
    export FAKE_DOCKER_LOG
    : >"$FAKE_DOCKER_LOG"
}

# Every recorded `docker` invocation, one per line.
docker_log() {
    cat "$FAKE_DOCKER_LOG" 2>/dev/null || true
}

# ── Fake LiteLLM ─────────────────────────────────────────────────────────────

# start_litellm [--mode MODE] [--seed-alias ALIAS ...]
#
# Exports LITELLM_PORT and LITELLM_LOG. Registers nothing: the caller's
# teardown must call stop_litellm (teardown() in each .bats file does).
start_litellm() {
    local port_file="${BATS_TEST_TMPDIR}/port"
    # Must be emptied first. A test that restarts the server on a different
    # mode would otherwise read the *previous* port still sitting in this
    # file, pass the readiness check instantly and then talk to a socket
    # nobody is listening on — which fails as "the proxy is down" and looks
    # like a defect in the script under test.
    rm -f "$port_file"
    LITELLM_LOG="${BATS_TEST_TMPDIR}/requests.jsonl"
    export LITELLM_LOG
    : >"$LITELLM_LOG"

    python3 "${HELPERS_DIR}/fake_litellm.py" "$port_file" \
        --log "$LITELLM_LOG" "$@" >/dev/null 2>&1 &
    FAKE_LITELLM_PID=$!
    export FAKE_LITELLM_PID

    # The server writes the port only after the socket is listening, so
    # polling this file is a race-free readiness check. Waiting on a fixed
    # sleep instead is how a suite becomes flaky on a loaded machine.
    local waited=0
    while [ ! -s "$port_file" ]; do
        sleep 0.05
        waited=$((waited + 1))
        if [ "$waited" -gt 200 ]; then
            echo "fake LiteLLM did not start within 10s" >&2
            return 1
        fi
        kill -0 "$FAKE_LITELLM_PID" 2>/dev/null || {
            echo "fake LiteLLM died during startup" >&2
            return 1
        }
    done
    LITELLM_PORT="$(cat "$port_file")"
    export LITELLM_PORT
}

stop_litellm() {
    if [ -n "${FAKE_LITELLM_PID:-}" ]; then
        kill "$FAKE_LITELLM_PID" 2>/dev/null || true
        wait "$FAKE_LITELLM_PID" 2>/dev/null || true
        unset FAKE_LITELLM_PID
    fi
}

# Requests the fake server received, as JSONL.
litellm_requests() {
    cat "$LITELLM_LOG" 2>/dev/null || true
}

# The JSON body of the first POST to a given path, or "" if there was none.
# Reading it with Python rather than grep keeps the assertion about the
# *parsed payload* — a test that greps for `"rpm_limit": 300` also passes when
# the field lands in the wrong object.
litellm_body_for() {
    local want="$1"
    python3 - "$LITELLM_LOG" "$want" <<'PY'
import json, sys
log, want = sys.argv[1], sys.argv[2]
try:
    with open(log, encoding="utf-8") as fh:
        for line in fh:
            entry = json.loads(line)
            if entry["method"] == "POST" and entry["path"] == want:
                print(json.dumps(entry["body"]))
                break
except FileNotFoundError:
    pass
PY
}

# jq is not a given on every machine; this reads one field out of a JSON blob.
json_field() {
    python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2], ""))' \
        "$1" "$2"
}

# ── Throwaway .env ───────────────────────────────────────────────────────────

# write_env [extra lines...] — writes $BATS_TEST_TMPDIR/.env and exports
# IDIA_ENV_FILE. Call after start_litellm so LITELLM_PORT is known.
write_env() {
    ENV_FILE="${BATS_TEST_TMPDIR}/.env"
    export IDIA_ENV_FILE="$ENV_FILE"
    {
        echo "LITELLM_MASTER_KEY=sk-test-master-key"
        echo "MODEL_ID=mistral-7b"
        echo "MODEL_SOURCE=mistralai/Mistral-7B-Instruct-v0.3"
        echo "IDIA_PUBLIC_HOST=idia.example.org"
        echo "OWUI_CONTAINER=idia-webui-test"
        [ -n "${LITELLM_PORT:-}" ] && echo "LITELLM_PORT=${LITELLM_PORT}"
        local line
        for line in "$@"; do
            echo "$line"
        done
    } >"$ENV_FILE"
}

# ── Invoking the scripts under test ──────────────────────────────────────────

colleague() {
    bash "${REPO_ROOT}/scripts/colleague.sh" "$@"
}

# ── A throwaway copy of the repo, for testing ./idia ─────────────────────────
#
# colleague.sh has a seam for this — IDIA_ENV_FILE — and ./idia does not: it
# hardcodes ENV_FILE="$REPO_DIR/.env". Testing its env-dependent paths in
# place would mean writing a .env into the working copy, on top of whatever
# the operator has there. So the tests run ./idia out of a temp directory
# instead.
#
# The scripts are COPIED, not symlinked, on purpose: render_config.py derives
# the repo root from Path(__file__).resolve().parent, and resolve() follows a
# symlink back to the real checkout — which is where --render-all would then
# write its output.
make_fake_repo() {
    FAKE_REPO="${BATS_TEST_TMPDIR}/repo"
    mkdir -p "$FAKE_REPO"
    cp "${REPO_ROOT}/idia" "$FAKE_REPO/idia"
    cp -R "${REPO_ROOT}/scripts" "$FAKE_REPO/scripts"
    cp "${REPO_ROOT}/docker-compose.yml" "$FAKE_REPO/docker-compose.yml"
    cp "${REPO_ROOT}/serve_config.yaml" "$FAKE_REPO/serve_config.yaml"
    export FAKE_REPO

    # ./idia reads $REPO_DIR/.env, so the file has to live in the copy.
    ENV_FILE="${FAKE_REPO}/.env"
    export IDIA_ENV_FILE="$ENV_FILE"
    {
        echo "LITELLM_MASTER_KEY=sk-test-master-key"
        echo "MODEL_ID=mistral-7b"
        echo "MODEL_SOURCE=mistralai/Mistral-7B-Instruct-v0.3"
        echo "IDIA_PUBLIC_HOST=idia.example.org"
        echo "OWUI_CONTAINER=idia-webui-test"
        [ -n "${LITELLM_PORT:-}" ] && echo "LITELLM_PORT=${LITELLM_PORT}"
        local line
        for line in "$@"; do
            echo "$line"
        done
    } >"$ENV_FILE"
}

idia() {
    bash "${FAKE_REPO:-$REPO_ROOT}/idia" "$@"
}
