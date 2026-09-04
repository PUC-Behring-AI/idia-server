#!/usr/bin/env bats
# The ./idia CLI — routing, deploy rendering, status, and the systemd wrappers.
#
# Everything runs against a temp copy of the repo (make_fake_repo) with a fake
# LiteLLM and a stub `docker`, so no test touches the operator's .env, their
# containers, or their launchd/systemd units.
#
# The assertions are mostly about the command line ./idia *builds*: whether
# the compose file is passed, whether the GPU profile flag appears, whether a
# subcommand reaches the script that implements it. That is where this CLI's
# defects have actually been — a flag that never got passed, a route that
# aborted on an unbound variable.

load helpers/common

setup() {
    stub_path
    start_litellm
    make_fake_repo
}

teardown() {
    stop_litellm
}

assert_ok() {
    if [ "$status" -ne 0 ]; then
        echo "expected exit 0, got $status" >&2
        echo "$output" >&2
        return 1
    fi
}

assert_fails() {
    if [ "$status" -eq 0 ]; then
        echo "expected a non-zero exit, got 0" >&2
        echo "$output" >&2
        return 1
    fi
}

assert_contains() {
    case "$output" in
        *"$1"*) : ;;
        *) echo "output does not contain '$1'" >&2; echo "$output" >&2; return 1 ;;
    esac
}

refute_contains() {
    case "$output" in
        *"$1"*) echo "output unexpectedly contains '$1'" >&2
                echo "$output" >&2; return 1 ;;
    esac
}

# ── Help and routing ─────────────────────────────────────────────────────────

@test "help exits zero and lists every top-level command" {
    run idia --help
    assert_ok
    for cmd in setup deploy status user colleague logs stop service; do
        assert_contains "$cmd"
    done
}

@test "no arguments prints help rather than failing silently" {
    run idia
    assert_contains "deploy"
}

@test "an unknown command is refused" {
    run idia naoexiste
    assert_fails
}

@test "deploy without a target is refused" {
    run idia deploy
    assert_fails
}

@test "deploy with an unknown target is refused" {
    run idia deploy marte
    assert_fails
}

@test "colleague --help routes through without an unbound variable" {
    # Regression guard: this route used to abort under `set -u` before it
    # reached colleague.sh at all.
    run idia colleague --help
    assert_ok
    refute_contains "unbound variable"
}

@test "user with an unknown subcommand is refused" {
    run idia user naoexiste
    assert_fails
}

@test "service with an unknown subcommand is refused" {
    run idia service naoexiste
    assert_fails
}

# ── deploy local --dry-run ───────────────────────────────────────────────────

@test "deploy local --dry-run renders both configs and starts nothing" {
    run idia deploy local --dry-run
    assert_ok
    [ -f "${FAKE_REPO}/rendered_serve_config.yaml" ]
    [ -f "${FAKE_REPO}/rendered_litellm_config.yaml" ]
    # Nothing may have been brought up. `docker compose version` from
    # _check_deps is expected; `up` is not.
    run bash -c "grep -c 'compose.*up' '$FAKE_DOCKER_LOG' || true"
    [ "$output" = "0" ]
}

@test "the rendered litellm config names the configured model" {
    run idia deploy local --dry-run
    assert_ok
    run grep -c "mistral-7b" "${FAKE_REPO}/rendered_litellm_config.yaml"
    assert_ok
}

@test "the rendered litellm config keeps the master key as an env reference" {
    run idia deploy local --dry-run
    assert_ok
    run grep -c "os.environ/LITELLM_MASTER_KEY" "${FAKE_REPO}/rendered_litellm_config.yaml"
    assert_ok
    run bash -c "grep -c 'sk-test-master-key' '${FAKE_REPO}/rendered_litellm_config.yaml' || true"
    [ "$output" = "0" ]
}

@test "deploy local fails when the .env is absent, naming the fix" {
    rm -f "${FAKE_REPO}/.env"
    run idia deploy local --dry-run
    assert_fails
    assert_contains "cp .env.example .env"
}

@test "deploy local fails when the .env has no model, not halfway through" {
    grep -v '^MODEL_ID=' "${FAKE_REPO}/.env" >"${FAKE_REPO}/.env.tmp"
    mv "${FAKE_REPO}/.env.tmp" "${FAKE_REPO}/.env"
    run idia deploy local --dry-run
    assert_fails
    assert_contains "rendering failed"
}

@test "a stale render is overwritten rather than reused" {
    echo "stale: true" >"${FAKE_REPO}/rendered_litellm_config.yaml"
    run idia deploy local --dry-run
    assert_ok
    run bash -c "grep -c 'stale' '${FAKE_REPO}/rendered_litellm_config.yaml' || true"
    [ "$output" = "0" ]
}

# ── status ───────────────────────────────────────────────────────────────────

@test "status runs with the compose file and does not crash" {
    run idia status
    assert_ok
    run bash -c "grep -c 'compose -f' '$FAKE_DOCKER_LOG'"
    assert_ok
    [ "$output" -ge 1 ]
}

@test "status warns instead of failing when LiteLLM is unreachable" {
    # A down proxy must not make `status` itself fail: the operator running it
    # is usually running it *because* something is down, and a non-zero exit
    # here would take the compose listing and the model list down with it.
    LITELLM_PORT=1 run idia status
    assert_ok
    assert_contains "not reachable"
}

@test "status reports LiteLLM healthy when it answers" {
    # LITELLM_PORT has to come through the ambient environment, not the .env:
    # ./idia freezes LITELLM_URL at startup from ${LITELLM_PORT:-4000}, before
    # _load_env sources the file. See the characterisation test below.
    LITELLM_PORT="$LITELLM_PORT" run idia status
    assert_ok
    assert_contains "healthy"
}

@test "LITELLM_PORT set only in the .env is ignored by status" {
    # Characterisation test, not an endorsement: ./idia computes LITELLM_URL
    # on line 58 from the ambient environment, and sources the .env on line
    # 136. A port configured in the .env therefore never reaches the health
    # check, which reports the server down while it is up. Tracked as an
    # issue; when it is fixed this test flips to asserting "healthy".
    unset LITELLM_PORT
    run idia status
    assert_ok
    assert_contains "not reachable"
}

# ── user ─────────────────────────────────────────────────────────────────────

@test "user create without a name is refused, offering the real tiers" {
    run idia user create
    assert_fails
    for tier in light regular heavy classroom; do
        assert_contains "$tier"
    done
}

@test "user create delegates to colleague and emits a key" {
    LITELLM_PORT="$LITELLM_PORT" run idia user create ana@idia.org light
    assert_ok
    assert_contains "sk-fake-"
}

@test "the legacy tier name 'hard' is mapped to 'heavy', out loud" {
    LITELLM_PORT="$LITELLM_PORT" run idia user create ana@idia.org hard
    assert_ok
    assert_contains "heavy"
}

@test "user create defaults to the regular tier" {
    LITELLM_PORT="$LITELLM_PORT" run idia user create ana@idia.org
    assert_ok
    body="$(litellm_body_for /key/generate)"
    [ "$(json_field "$body" team_id)" = "regular" ]
}

@test "user list warns and fails when the proxy is unreachable" {
    LITELLM_PORT=1 run idia user list
    assert_fails
    assert_contains "Could not fetch key list"
}

@test "user list shows the aliases that exist" {
    LITELLM_PORT="$LITELLM_PORT" run idia user create ana@idia.org light
    assert_ok
    LITELLM_PORT="$LITELLM_PORT" run idia user list
    assert_ok
    assert_contains "ana"
}

@test "user list accepts an explicit endpoint" {
    run idia user list "http://127.0.0.1:${LITELLM_PORT}"
    assert_ok
}

# ── logs and stop ────────────────────────────────────────────────────────────

@test "stop brings the stack down through compose" {
    run idia stop
    assert_ok
    run bash -c "grep -c 'compose -f.*down' '$FAKE_DOCKER_LOG'"
    assert_ok
    [ "$output" -ge 1 ]
}

@test "stop tells the operator that volumes survive" {
    run idia stop
    assert_ok
    assert_contains "volumes"
}

@test "logs without a service tails everything" {
    run idia logs
    assert_ok
    run bash -c "grep -c 'compose -f.*logs -f$' '$FAKE_DOCKER_LOG'"
    assert_ok
}

@test "logs with a service tails just that one" {
    run idia logs litellm
    assert_ok
    run bash -c "grep -c 'logs -f litellm' '$FAKE_DOCKER_LOG'"
    assert_ok
}

# ── service (systemd/launchd wrappers) ───────────────────────────────────────

@test "service status falls back to the stack listing with no unit installed" {
    # With no systemd unit — every macOS machine, and any Linux box where the
    # service was never installed — it reports the containers instead of
    # failing. Worth pinning: the fallback is silent, so a reader of the
    # output cannot tell which of the two questions was answered.
    run idia service status
    assert_ok
    run bash -c "grep -c 'compose -f.*ps' '$FAKE_DOCKER_LOG'"
    assert_ok
    [ "$output" -ge 1 ]
}
