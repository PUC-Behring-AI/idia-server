#!/usr/bin/env bats
# status, revoke and tiers — the commands an operator runs after provisioning.
#
# revoke is the one that matters most and had no test at all: it deletes keys
# in LiteLLM *and* the user in Open WebUI, and it does the LiteLLM half first.
# If the second half fails the account keeps working with no key, which is a
# state neither screen shows.

load helpers/common

setup() {
    stub_path
    start_litellm
    write_env
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

# ── tiers ────────────────────────────────────────────────────────────────────

@test "tiers lists all four with their limits" {
    run colleague tiers
    assert_ok
    for tier in light regular heavy classroom; do
        assert_contains "$tier"
    done
    assert_contains "0.50"
    assert_contains "200000"
}

@test "tiers shows heavy as unlimited rather than blank" {
    run colleague tiers
    assert_ok
    assert_contains "ilimitado"
}

@test "tiers echoes the models from the .env, not a baked-in name" {
    write_env "MODELS_COUNT=1" "MODEL_1_ID=qwen3-8b"
    run colleague tiers
    assert_ok
    assert_contains "qwen3-8b"
}

@test "tiers on an .env with no model is refused" {
    grep -v '^MODEL_ID=' "$IDIA_ENV_FILE" >"${IDIA_ENV_FILE}.tmp"
    mv "${IDIA_ENV_FILE}.tmp" "$IDIA_ENV_FILE"
    run colleague tiers
    assert_fails
    assert_contains "Nenhum modelo configurado"
}

# ── status ───────────────────────────────────────────────────────────────────

@test "status without an email is refused with usage" {
    run colleague status
    assert_fails
    assert_contains "Uso:"
}

@test "status reports a container that is not up, without failing" {
    # A down Open WebUI must not stop the LiteLLM half from being reported:
    # the operator asking for status is usually asking *because* something
    # is down.
    run colleague status ana@idia.org
    assert_ok
    assert_contains "não está no ar"
    assert_contains "LiteLLM"
}

@test "status reports an existing key's budget and spend" {
    run colleague key ana@idia.org
    assert_ok
    run colleague status ana@idia.org
    assert_ok
    assert_contains "Alias:"
    assert_contains "ana"
    assert_contains "Budget:"
}

@test "status says so when the alias has no key" {
    run colleague status ninguem@idia.org
    assert_ok
    assert_contains "nenhuma key encontrada"
}

@test "status survives an unreachable LiteLLM with a warning" {
    write_env "LITELLM_PORT=1"
    run colleague status ana@idia.org
    assert_ok
    assert_contains "LiteLLM indisponível"
}

@test "status queries Open WebUI when the container is present" {
    export FAKE_DOCKER_CONTAINERS="idia-webui-test"
    run colleague status ana@idia.org
    assert_ok
    run bash -c "grep -c exec '$FAKE_DOCKER_LOG'"
    [ "$output" -ge 1 ]
}

# ── revoke ───────────────────────────────────────────────────────────────────

@test "revoke without an email is refused with usage" {
    run colleague revoke
    assert_fails
    assert_contains "Uso:"
}

@test "revoke deletes the LiteLLM key and reports how many" {
    export FAKE_DOCKER_CONTAINERS="idia-webui-test"
    run colleague key ana@idia.org
    assert_ok
    run colleague revoke ana@idia.org
    assert_ok
    assert_contains "revogada"
    # And the key is really gone: a second revoke finds nothing.
    run colleague revoke ana@idia.org
    assert_ok
    assert_contains "Nenhuma key LiteLLM"
}

@test "revoke says so when there was nothing to revoke" {
    export FAKE_DOCKER_CONTAINERS="idia-webui-test"
    run colleague revoke ninguem@idia.org
    assert_ok
    assert_contains "Nenhuma key LiteLLM"
}

@test "revoke leaves other aliases' keys alone" {
    export FAKE_DOCKER_CONTAINERS="idia-webui-test"
    run colleague key ana@idia.org
    assert_ok
    run colleague key bruno@idia.org
    assert_ok
    run colleague revoke ana@idia.org
    assert_ok
    run colleague status bruno@idia.org
    assert_ok
    assert_contains "bruno"
}

@test "revoke refuses when the Open WebUI container is absent" {
    # It refuses *after* deleting the LiteLLM keys — so the account survives
    # with no key. Recorded here as the behaviour that exists, with the
    # ordering problem tracked as an issue rather than fixed in a test diff.
    run colleague revoke ana@idia.org
    assert_fails
    assert_contains "idia-webui-test"
}

@test "revoke fails loudly when the Open WebUI call fails" {
    export FAKE_DOCKER_CONTAINERS="idia-webui-test"
    export FAKE_DOCKER_FAIL="exec"
    run colleague revoke ana@idia.org
    assert_fails
    assert_contains "Falha ao remover"
}

# ── help and routing ─────────────────────────────────────────────────────────

@test "help exits zero and names every subcommand" {
    run colleague --help
    assert_ok
    for sub in create key status revoke tiers; do
        assert_contains "$sub"
    done
}

@test "an unknown subcommand is refused" {
    run colleague naoexiste
    assert_fails
}

@test "no subcommand at all prints help rather than crashing" {
    run colleague
    assert_contains "Uso:"
}

@test "IDIA_PROG renames the program in usage messages" {
    # ./idia sets this so the usage text says "./idia colleague", not
    # "colleague.sh" — a path the user never types.
    IDIA_PROG="./idia colleague" run colleague status
    assert_fails
    assert_contains "./idia colleague"
}
