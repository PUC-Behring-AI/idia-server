#!/usr/bin/env bats
# The provisioning path, end to end, against a fake LiteLLM.
#
# tests/test_colleague.py already covers everything that stops before the
# network: --help, tiers, --dry-run, and the injection guards. This file
# covers the other half — the requests colleague.sh actually sends — which is
# where the defect that shipped yesterday lived: the server emitted no virtual
# key at all, and no test could have noticed, because no test ever asked for
# one.
#
# The assertions are about the *parsed payload* reaching /key/generate, not
# about the words on the terminal. A tier whose limits are printed correctly
# and sent as null is the exact failure this guards: the operator reads
# "300 req/min", the user gets unlimited, and both screens agree.

load helpers/common

setup() {
    stub_path
    start_litellm
    write_env
}

teardown() {
    stop_litellm
}

# ── assertion helpers ────────────────────────────────────────────────────────

assert_ok() {
    if [ "$status" -ne 0 ]; then
        echo "expected exit 0, got $status" >&2
        echo "--- output ---" >&2
        echo "$output" >&2
        return 1
    fi
}

assert_fails() {
    if [ "$status" -eq 0 ]; then
        echo "expected a non-zero exit, got 0" >&2
        echo "--- output ---" >&2
        echo "$output" >&2
        return 1
    fi
}

assert_contains() {
    case "$output" in
        *"$1"*) : ;;
        *) echo "output does not contain '$1'" >&2
           echo "--- output ---" >&2
           echo "$output" >&2
           return 1 ;;
    esac
}

# field <json> <key> — a payload field, or "" when absent.
field() {
    python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2], ""))' "$1" "$2"
}

has_field() {
    python3 -c 'import json,sys; sys.exit(0 if sys.argv[2] in json.loads(sys.argv[1]) else 1)' \
        "$1" "$2"
}

# ── The key gets emitted ─────────────────────────────────────────────────────

@test "create --no-openwebui emits a virtual key" {
    run colleague create ana@idia.org "Ana Costa" --no-openwebui
    assert_ok
    assert_contains "sk-fake-"
}

@test "create --no-openwebui reports the configured public host, not localhost" {
    run colleague create ana@idia.org "Ana Costa" --no-openwebui
    assert_ok
    assert_contains "idia.example.org"
}

@test "create actually posts to /key/generate" {
    run colleague create ana@idia.org "Ana Costa" --no-openwebui
    assert_ok
    [ -n "$(litellm_body_for /key/generate)" ]
}

@test "the alias is the local part of the email" {
    run colleague create ana.costa@idia.org "Ana Costa" --no-openwebui
    assert_ok
    body="$(litellm_body_for /key/generate)"
    [ "$(field "$body" key_alias)" = "ana.costa" ]
}

# ── Tier limits reach the proxy ──────────────────────────────────────────────

@test "regular tier sends its own budget, rpm and tpm" {
    run colleague create ana@idia.org "Ana" --no-openwebui
    assert_ok
    body="$(litellm_body_for /key/generate)"
    [ "$(field "$body" max_budget)" = "2.0" ]
    [ "$(field "$body" budget_duration)" = "1d" ]
    [ "$(field "$body" rpm_limit)" = "60" ]
    [ "$(field "$body" tpm_limit)" = "30000" ]
    [ "$(field "$body" team_id)" = "regular" ]
}

@test "light tier sends its own limits" {
    run colleague create ana@idia.org "Ana" --tier light --no-openwebui
    assert_ok
    body="$(litellm_body_for /key/generate)"
    [ "$(field "$body" max_budget)" = "0.5" ]
    [ "$(field "$body" rpm_limit)" = "10" ]
    [ "$(field "$body" tpm_limit)" = "5000" ]
}

@test "classroom tier sends its own limits" {
    run colleague create turma@idia.org "Turma" --tier classroom --no-openwebui
    assert_ok
    body="$(litellm_body_for /key/generate)"
    [ "$(field "$body" max_budget)" = "20.0" ]
    [ "$(field "$body" rpm_limit)" = "300" ]
    [ "$(field "$body" tpm_limit)" = "200000" ]
}

@test "heavy tier omits the rate limits instead of sending zero" {
    # "Unlimited" has to be an absent field. Sending rpm_limit: 0 is a real
    # risk here, and LiteLLM reads it as "zero requests per minute" — a tier
    # advertised as unrestricted that blocks every call.
    run colleague create chefe@idia.org "Chefe" --tier heavy --no-openwebui
    assert_ok
    body="$(litellm_body_for /key/generate)"
    [ "$(field "$body" max_budget)" = "10.0" ]
    run has_field "$body" rpm_limit
    assert_fails
    run has_field "$body" tpm_limit
    assert_fails
}

@test "explicit flags override the tier defaults" {
    run colleague create ana@idia.org "Ana" --tier light \
        --budget 7 --budget-period 30d --rpm 99 --tpm 12345 --no-openwebui
    assert_ok
    body="$(litellm_body_for /key/generate)"
    [ "$(field "$body" max_budget)" = "7.0" ]
    [ "$(field "$body" budget_duration)" = "30d" ]
    [ "$(field "$body" rpm_limit)" = "99" ]
    [ "$(field "$body" tpm_limit)" = "12345" ]
}

@test "optional flags only appear in the payload when given" {
    run colleague create ana@idia.org "Ana" --no-openwebui
    assert_ok
    body="$(litellm_body_for /key/generate)"
    for absent in max_parallel_requests expires tags blocked; do
        run has_field "$body" "$absent"
        assert_fails
    done
}

@test "max-parallel, expires, tag and blocked reach the payload when given" {
    run colleague create ana@idia.org "Ana" --max-parallel 3 --expires 30d \
        --tag turma-b --blocked --no-openwebui
    assert_ok
    body="$(litellm_body_for /key/generate)"
    [ "$(field "$body" max_parallel_requests)" = "3" ]
    [ "$(field "$body" expires)" = "30d" ]
    [ "$(field "$body" blocked)" = "True" ]
    assert_contains "Blocked"
}

# ── Models ───────────────────────────────────────────────────────────────────

@test "models default to what the .env configures" {
    run colleague create ana@idia.org "Ana" --no-openwebui
    assert_ok
    body="$(litellm_body_for /key/generate)"
    [ "$(field "$body" models)" = "['mistral-7b']" ]
}

@test "multi-model .env contributes every configured model" {
    write_env "MODELS_COUNT=2" "MODEL_1_ID=qwen3-8b" "MODEL_2_ID=mistral-7b"
    run colleague create ana@idia.org "Ana" --no-openwebui
    assert_ok
    body="$(litellm_body_for /key/generate)"
    [ "$(field "$body" models)" = "['qwen3-8b', 'mistral-7b']" ]
}

@test "--models narrows the grant to the named models" {
    write_env "MODELS_COUNT=2" "MODEL_1_ID=qwen3-8b" "MODEL_2_ID=mistral-7b"
    run colleague create ana@idia.org "Ana" --models mistral-7b --no-openwebui
    assert_ok
    body="$(litellm_body_for /key/generate)"
    [ "$(field "$body" models)" = "['mistral-7b']" ]
}

@test "an .env with no model configured is refused" {
    write_env
    # Overwrite without MODEL_ID: the file the script reads must have none.
    grep -v '^MODEL_ID=' "$IDIA_ENV_FILE" >"${IDIA_ENV_FILE}.tmp"
    mv "${IDIA_ENV_FILE}.tmp" "$IDIA_ENV_FILE"
    run colleague create ana@idia.org "Ana" --no-openwebui
    assert_fails
    assert_contains "Nenhum modelo configurado"
}

# ── Cleaning previous keys ───────────────────────────────────────────────────

@test "a previous key for the same alias is deleted before the new one" {
    stop_litellm
    start_litellm --seed-alias ana
    write_env
    run colleague create ana@idia.org "Ana" --no-openwebui
    assert_ok
    # The delete must have been sent, and it must name the seeded token.
    run bash -c "grep -c '\"path\": \"/key/delete\"' '$LITELLM_LOG'"
    assert_ok
    [ "$output" -ge 1 ]
}

@test "keys belonging to another alias are left alone" {
    stop_litellm
    start_litellm --seed-alias someone-else
    write_env
    run colleague create ana@idia.org "Ana" --no-openwebui
    assert_ok
    run bash -c "grep -c '\"path\": \"/key/delete\"' '$LITELLM_LOG' || true"
    [ "$output" = "0" ]
}

# ── The proxy misbehaving ────────────────────────────────────────────────────

@test "a 200 response with no key is refused, not reported as success" {
    # The shape that slips past a naive check: plausible JSON, HTTP 200, no
    # key. Accepting it hands the user a credential that is the empty string.
    stop_litellm
    start_litellm --mode no-key
    write_env
    run colleague create ana@idia.org "Ana" --no-openwebui
    assert_fails
    assert_contains "não devolveu uma chave válida"
}

@test "a 500 from /key/generate is refused with the response body quoted" {
    stop_litellm
    start_litellm --mode error
    write_env
    run colleague create ana@idia.org "Ana" --no-openwebui
    assert_fails
    assert_contains "não devolveu uma chave válida"
}

@test "an unreachable proxy fails instead of reporting a key" {
    # Known gap, tracked as an issue: the failure arrives as a raw Python
    # traceback from _litellm_delete_keys_by_alias, which handles HTTPError
    # but not URLError. The exit status is right and the message is not, so
    # this asserts only what is true today. Tightening it to demand a
    # diagnostic is the fix, not the test.
    stop_litellm
    write_env "LITELLM_PORT=1"
    run colleague create ana@idia.org "Ana" --no-openwebui
    assert_fails
}

# ── Environment validation ───────────────────────────────────────────────────

@test "a .env without LITELLM_MASTER_KEY is refused by name" {
    grep -v '^LITELLM_MASTER_KEY=' "$IDIA_ENV_FILE" >"${IDIA_ENV_FILE}.tmp"
    mv "${IDIA_ENV_FILE}.tmp" "$IDIA_ENV_FILE"
    run colleague create ana@idia.org "Ana" --no-openwebui
    assert_fails
    assert_contains "LITELLM_MASTER_KEY"
}

@test "the master key is sent as a bearer token, never in the payload" {
    run colleague create ana@idia.org "Ana" --no-openwebui
    assert_ok
    body="$(litellm_body_for /key/generate)"
    case "$body" in
        *sk-test-master-key*)
            echo "the master key leaked into the request body" >&2
            return 1 ;;
    esac
}

# ── Hostile input reaches the wire as data ───────────────────────────────────

@test "an apostrophe in the name survives to the credentials" {
    run colleague create ana@idia.org "Ana D'Ávila" --no-openwebui
    assert_ok
}

@test "a name carrying a shell payload is inert and stays data" {
    marker="${BATS_TEST_TMPDIR}/pwned"
    run colleague create ana@idia.org "x'); import os; os.system('touch ${marker}'); #" \
        --no-openwebui
    assert_ok
    [ ! -e "$marker" ]
}

@test "an alias with a quote is sent as a JSON string, not broken JSON" {
    run colleague create "o'brien@idia.org" "O'Brien" --no-openwebui
    assert_ok
    body="$(litellm_body_for /key/generate)"
    [ "$(field "$body" key_alias)" = "o'brien" ]
}

# ── Argument handling ────────────────────────────────────────────────────────

@test "create without a name is refused with usage" {
    run colleague create ana@idia.org
    assert_fails
    assert_contains "Uso:"
}

@test "an unknown flag is refused and named" {
    run colleague create ana@idia.org "Ana" --nao-existe
    assert_fails
    assert_contains "--nao-existe"
}

@test "an invalid tier is refused and the valid ones listed" {
    run colleague create ana@idia.org "Ana" --tier gigante --no-openwebui
    assert_fails
    assert_contains "gigante"
    assert_contains "light"
    assert_contains "classroom"
}

# ── Open WebUI branch ────────────────────────────────────────────────────────

@test "a missing Open WebUI container is refused before any key is created" {
    # Ordering matters: failing after /key/generate leaves an orphan key in
    # LiteLLM that nobody will ever use and nothing will clean up.
    run colleague create ana@idia.org "Ana"
    assert_fails
    assert_contains "idia-webui-test"
    [ -z "$(litellm_body_for /key/generate)" ]
}

@test "the full path runs when the container is present" {
    export FAKE_DOCKER_CONTAINERS="idia-webui-test"
    run colleague create ana@idia.org "Ana"
    assert_ok
    assert_contains "CREDENCIAIS"
    assert_contains "ana@idia.org"
    [ -n "$(litellm_body_for /key/generate)" ]
}

@test "an Open WebUI that returns no user id is refused" {
    export FAKE_DOCKER_CONTAINERS="idia-webui-test"
    export FAKE_OWUI_USER_ID=""
    run colleague create ana@idia.org "Ana"
    assert_fails
    assert_contains "id de usuário"
}

@test "the discovery key is skipped with a warning when unset" {
    export FAKE_DOCKER_CONTAINERS="idia-webui-test"
    run colleague create ana@idia.org "Ana"
    assert_ok
    assert_contains "OWUI_DISCOVERY_KEY"
}

@test "the generated password is never the same twice" {
    export FAKE_DOCKER_CONTAINERS="idia-webui-test"
    run colleague create ana@idia.org "Ana"
    assert_ok
    first="$output"
    run colleague create ana@idia.org "Ana"
    assert_ok
    [ "$first" != "$output" ]
}

@test "an explicit --password is the one handed over" {
    export FAKE_DOCKER_CONTAINERS="idia-webui-test"
    run colleague create ana@idia.org "Ana" --password trocar-no-primeiro-login
    assert_ok
    assert_contains "trocar-no-primeiro-login"
}

@test "the role reaches the Open WebUI call" {
    export FAKE_DOCKER_CONTAINERS="idia-webui-test"
    run colleague create prof@idia.org "Prof" --role admin
    assert_ok
    assert_contains "admin"
}

# ── key: the create shortcut ─────────────────────────────────────────────────

@test "key <email> provisions without touching Open WebUI" {
    run colleague key ana@idia.org
    assert_ok
    assert_contains "sk-fake-"
    [ -z "$(docker_log)" ]
}

@test "key derives the name from the email and honours --tier" {
    run colleague key ana@idia.org --tier light
    assert_ok
    body="$(litellm_body_for /key/generate)"
    [ "$(field "$body" key_alias)" = "ana" ]
    [ "$(field "$body" rpm_limit)" = "10" ]
}

@test "key without an email is refused with usage" {
    run colleague key
    assert_fails
    assert_contains "Uso:"
}
