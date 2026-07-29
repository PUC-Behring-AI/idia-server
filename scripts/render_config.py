#!/usr/bin/env python3
"""Render serve_config.yaml + config.yaml with env var substitution.

Usage (via Dockerfile CMD):
    python3 /app/render_config.py

Workflow:
    1. Read serve_config.yaml as a template with ${VAR} placeholders.
    2. Substitute each placeholder from the corresponding environment variable.
    3. Write the rendered YAML to a fixed path (/tmp/idia_serve_config.yaml).
    4. exec serve run on the rendered file (replaces this process).

Flags:
    --dry-run       Render serve_config only to stdout. No files written.
                    No Ray Serve launched. Useful for config validation.
    --render-all    Render BOTH serve_config and litellm_config to repo root.
                    Used by ``./idia deploy local`` before ``docker compose up``.

Required env vars:
    MODEL_ID          — Short model alias (e.g. "mistral-7b")
    MODEL_SOURCE      — HuggingFace Hub identifier (e.g. "mistralai/Mistral-7B-Instruct-v0.3")

Optional env vars (with defaults):
    MAX_MODEL_LEN           = 8192
    GPU_MEMORY_UTILIZATION  = 0.9

Testing:
    Call this module directly: ``python3 -m scripts.render_config --dry-run``
    to render and validate without launching Ray Serve.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml


# ── Env var schema ──────────────────────────────────────────────────────────

# (type, default_or_None_if_required)
ENV_SCHEMA: dict[str, tuple[type, object]] = {
    "MODEL_ID": (str, None),  # None = required (single-model mode)
    "MODEL_SOURCE": (str, None),
    "MAX_MODEL_LEN": (int, 8192),
    "GPU_MEMORY_UTILIZATION": (float, 0.9),
    "MODELS_COUNT": (int, None),  # set >0 for multi-model mode
    "GPU_COUNT": (int, 1),  # number of GPUs available (for VRAM budget validation)
    "GPU_VRAM_GB": (float, 24.0),  # VRAM per GPU in GB (A10G = 24, A100 = 40/80)
}

TEMPLATE_FILENAME = "serve_config.yaml"
RENDERED_PATH = Path("/tmp/idia_serve_config.yaml")

# Placeholder pattern for env var substitution
ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")

# Characters in values that would corrupt YAML structure
YAML_SPECIAL_CHARS = set(":{}\n#")

# Multi-model helper: model config template used for each numbered entry
MODEL_CONFIG_TEMPLATE = """        - model_loading_config:
            model_id: {model_id}
            model_source: {model_source}
          engine_kwargs:
            dtype: bfloat16
            gpu_memory_utilization: {gpu_util}
            max_model_len: {max_len}
          deployment_config:
            health_check_period_s: 30
            health_check_timeout_s: 10
            autoscaling_config:
              min_replicas: 0
              max_replicas: 4
              target_ongoing_requests: 64"""

LLM_CONFIGS_MARKER = "##LLM_CONFIGS##"

# Output filename for the rendered LiteLLM config (written to repo root by --render-all)
LITELLM_RENDERED_FILENAME = "rendered_litellm_config.yaml"
# LiteLLM env-var reference syntax — keeps secrets out of rendered files on disk
_LITELLM_MASTER_KEY_REF = "os.environ/LITELLM_MASTER_KEY"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _find_template(caller_dir: Path | None = None) -> Path:
    """Locate the template YAML, searching caller dir, parent dir, then /app."""
    candidates = []
    if caller_dir is not None:
        candidates.append(caller_dir / TEMPLATE_FILENAME)
        candidates.append(caller_dir.parent / TEMPLATE_FILENAME)
        candidates.append(caller_dir.parent.parent / TEMPLATE_FILENAME)
    candidates.append(Path("/app") / TEMPLATE_FILENAME)

    for p in candidates:
        if p.is_file():
            return p

    searched = ", ".join(str(p) for p in candidates)
    print(f"FATAL: {TEMPLATE_FILENAME} not found (searched: {searched})", file=sys.stderr)
    sys.exit(1)


def _read_file(path: Path) -> str:
    """Read a text file with explicit error handling.

    Raises SystemExit on common I/O errors with actionable messages.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(
            f"FATAL: Arquivo não encontrado: {path}\n"
            f"  Verifique se o arquivo existe e o path está correto.",
            file=sys.stderr,
        )
        sys.exit(1)
    except PermissionError:
        print(
            f"FATAL: Permissão negada: {path}\n"
            f"  Verifique as permissões de leitura do arquivo.",
            file=sys.stderr,
        )
        sys.exit(1)
    except UnicodeDecodeError as e:
        print(
            f"FATAL: Erro de encoding em {path}: {e}\n"
            f"  O arquivo deve ser UTF-8. Verifique o encoding.",
            file=sys.stderr,
        )
        sys.exit(1)


def _apply_defaults(env: dict[str, str]) -> None:
    """Inject schema defaults for optional env vars not present in *env*."""
    for var, (_, default) in ENV_SCHEMA.items():
        if default is not None and var not in env:
            env[var] = str(default)


def _validate_schema_values(env: dict[str, str]) -> None:
    """Validate env var values against schema constraints.

    Currently validates:
      - GPU_MEMORY_UTILIZATION: must be float in (0, 1]
      - MAX_MODEL_LEN: must be a positive integer string
      - GPU_COUNT: must be a positive integer
      - GPU_VRAM_GB: must be a positive float
      - Multi-model VRAM budget: total VRAM <= available GPU VRAM

    Exits with code 1 on validation failure.
    """
    # GPU_MEMORY_UTILIZATION range validation
    gpu_util_str = env.get("GPU_MEMORY_UTILIZATION", "0.9")
    try:
        gpu_util = float(gpu_util_str)
        if not (0 < gpu_util <= 1.0):
            print(
                f"FATAL: GPU_MEMORY_UTILIZATION deve estar entre 0 e 1, "
                f"recebido '{gpu_util_str}'",
                file=sys.stderr,
            )
            sys.exit(1)
    except ValueError:
        print(
            f"FATAL: GPU_MEMORY_UTILIZATION deve ser um número float, "
            f"recebido '{gpu_util_str}'",
            file=sys.stderr,
        )
        sys.exit(1)

    # MAX_MODEL_LEN format validation
    max_len_str = env.get("MAX_MODEL_LEN", "8192")
    if not max_len_str.isdigit() or int(max_len_str) <= 0:
        print(
            f"FATAL: MAX_MODEL_LEN deve ser um inteiro positivo, "
            f"recebido '{max_len_str}'",
            file=sys.stderr,
        )
        sys.exit(1)

    # GPU_COUNT validation
    gpu_count_str = env.get("GPU_COUNT", "1")
    try:
        gpu_count = int(gpu_count_str)
        if gpu_count < 1:
            print(
                f"FATAL: GPU_COUNT deve ser >= 1, recebido '{gpu_count_str}'",
                file=sys.stderr,
            )
            sys.exit(1)
    except ValueError:
        print(
            f"FATAL: GPU_COUNT deve ser um inteiro, recebido '{gpu_count_str}'",
            file=sys.stderr,
        )
        sys.exit(1)

    # GPU_VRAM_GB validation
    vram_str = env.get("GPU_VRAM_GB", "24")
    try:
        vram_gb = float(vram_str)
        if vram_gb <= 0:
            print(
                f"FATAL: GPU_VRAM_GB deve ser > 0, recebido '{vram_str}'",
                file=sys.stderr,
            )
            sys.exit(1)
    except ValueError:
        print(
            f"FATAL: GPU_VRAM_GB deve ser um número, recebido '{vram_str}'",
            file=sys.stderr,
        )
        sys.exit(1)

    # Multi-model VRAM budget: ensure models fit in available GPUs
    mc_str = env.get("MODELS_COUNT", "0")
    if mc_str:
        try:
            models_count = int(mc_str)
        except ValueError:
            return  # already validated in _collect_env

        if models_count > 0:
            total_util = models_count * gpu_util
            if total_util > gpu_count:
                print(
                    f"FATAL: {models_count} modelo(s) com "
                    f"GPU_MEMORY_UTILIZATION={gpu_util:.1f} cada = "
                    f"{total_util:.1f}x GPU, mas GPU_COUNT={gpu_count}. "
                    f"Reduza GPU_MEMORY_UTILIZATION ou aumente GPU_COUNT.",
                    file=sys.stderr,
                )
                sys.exit(1)


def _collect_env() -> dict[str, str]:
    """Validate and collect env vars, injecting defaults for optionals.

    For multi-model mode (MODELS_COUNT > 0), also collects MODEL_N_ID
    and MODEL_N_SOURCE for N = 1..MODELS_COUNT.

    Returns a flat dict of all vars needed for substitution.
    Exits with code 1 if any required var is missing.
    """
    env = dict(os.environ)
    missing: list[str] = []

    # Parse MODELS_COUNT early for multi-model validation
    models_count = 0
    mc_str = env.get("MODELS_COUNT", "")
    if mc_str:
        try:
            models_count = int(mc_str)
        except ValueError:
            print(
                f"FATAL: MODELS_COUNT deve ser um inteiro, recebido '{mc_str}'",
                file=sys.stderr,
            )
            sys.exit(1)

    if models_count > 0:
        # Multi-model mode: require individual entries, not single MODEL_ID
        for n in range(1, models_count + 1):
            for suffix in ("_ID", "_SOURCE"):
                var = f"MODEL_{n}{suffix}"
                if var not in env:
                    missing.append(var)
    else:
        # Single-model mode: require MODEL_ID and MODEL_SOURCE
        for var, (typ, default) in ENV_SCHEMA.items():
            if default is None and var != "MODELS_COUNT":
                if var not in env:
                    missing.append(var)

    if missing:
        print(f"FATAL: Required env var(s) not set: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    _apply_defaults(env)
    _validate_schema_values(env)
    return env


def _escape_yaml_value(value: str) -> str:
    """Escape a value for safe YAML substitution.

    If the value contains YAML special characters (:, {, }, \\n, #),
    serialize it as a quoted YAML scalar. Otherwise return as-is.
    """
    if any(c in value for c in YAML_SPECIAL_CHARS):
        return yaml.dump(value, default_style='"').strip().rstrip("\n...")
    return value


def _build_llm_configs(env: dict[str, str]) -> str:
    """Build the multi-model llm_configs YAML block from env vars.

    If MODELS_COUNT > 0, generates numbered entries using MODEL N_ID/SOURCE.
    Otherwise, returns empty string (single-model mode uses the template's
    fallback entry).
    """
    mc_str = env.get("MODELS_COUNT", "0")
    try:
        models_count = int(mc_str) if mc_str else 0
    except ValueError:
        models_count = 0

    if models_count < 1:
        return ""  # single-model mode — keep template fallback entry

    gpu_util = env.get("GPU_MEMORY_UTILIZATION", "0.9")
    max_len = env.get("MAX_MODEL_LEN", "8192")
    entries: list[str] = []

    for n in range(1, models_count + 1):
        model_id = env.get(f"MODEL_{n}_ID", "")
        model_source = env.get(f"MODEL_{n}_SOURCE", "")
        if not model_id or not model_source:
            # Silently skip incomplete entries
            continue
        entries.append(
            MODEL_CONFIG_TEMPLATE.format(
                model_id=model_id,
                model_source=model_source,
                gpu_util=gpu_util,
                max_len=max_len,
            )
        )

    return "\n".join(entries)


def _substitute(raw: str, env: dict[str, str]) -> str:
    """Replace ${VAR} placeholders with values from *env*.

    Also handles the ##LLM_CONFIGS## marker for multi-model support:
    - If MODELS_COUNT > 0: generates numbered model entries from env vars
      and removes the fallback single-model entry below the marker.
    - Otherwise: replaces the marker with empty string, keeping the
      fallback entry for single-model backward compatibility.

    Values containing YAML special characters are automatically escaped.
    Unknown placeholders are left untouched so they produce an obvious
    error when Ray Serve tries to parse the rendered YAML.
    """
    # Handle multi-model marker first
    if LLM_CONFIGS_MARKER in raw:
        mc_str = env.get("MODELS_COUNT", "0")
        try:
            models_count = int(mc_str) if mc_str else 0
        except ValueError:
            models_count = 0

        if models_count > 0:
            # Multi-model: replace marker with generated entries, skip fallback
            generated = _build_llm_configs(env)
            lines = raw.split("\n")
            new_lines = []
            skip_until_section = False
            for line in lines:
                if LLM_CONFIGS_MARKER in line:
                    # Keep the llm_configs: key, replace marker with content
                    prefix = line.split(LLM_CONFIGS_MARKER)[0]
                    new_lines.append(f"{prefix}\n{generated}")
                    skip_until_section = True
                    continue
                if skip_until_section:
                    # Skip fallback entry lines (indented >= 4 spaces)
                    # Stop when reaching a line at 0-2 space indent (new section)
                    indent = len(line) - len(line.lstrip())
                    if indent <= 2 and line.strip():
                        skip_until_section = False
                        new_lines.append(line)
                    continue
                new_lines.append(line)
            raw = "\n".join(new_lines)
        else:
            # Single-model: just remove the marker, keep fallback entry
            raw = raw.replace(LLM_CONFIGS_MARKER, "")

    def _replacer(match: re.Match) -> str:
        name = match.group(1)
        raw_match: str = match.group(0) or match.string[match.start() : match.end()]
        value = env.get(name, raw_match)
        return _escape_yaml_value(value)

    return ENV_VAR_RE.sub(_replacer, raw)


def _validate_yaml(rendered: str) -> None:
    """Parse rendered YAML; exit with diagnostic on failure."""
    try:
        parsed = yaml.safe_load(rendered)
    except yaml.YAMLError as e:
        print(f"FATAL: Rendered {TEMPLATE_FILENAME} is invalid YAML:\n{e}", file=sys.stderr)
        sys.exit(1)

    if parsed is None:
        print(f"FATAL: Rendered {TEMPLATE_FILENAME} is empty", file=sys.stderr)
        sys.exit(1)

    # Structural validation against expected schema
    apps = parsed.get("applications", [])
    if not apps:
        print(f"FATAL: Rendered YAML has no 'applications' list", file=sys.stderr)
        sys.exit(1)

    llm_configs = apps[0].get("args", {}).get("llm_configs", [])
    if not llm_configs:
        print(f"FATAL: No llm_configs found in first application entry", file=sys.stderr)
        sys.exit(1)

    # Validate each llm_config has model_id and model_source
    for i, cfg in enumerate(llm_configs):
        mlc = cfg.get("model_loading_config", {})
        if not mlc.get("model_id") or not mlc.get("model_source"):
            print(
                f"FATAL: llm_config[{i}] (model_id='{mlc.get('model_id')}', "
                f"model_source='{mlc.get('model_source')}') "
                f"missing model_id or model_source after substitution",
                file=sys.stderr,
            )
            sys.exit(1)


def _log_diagnostics(env: dict[str, str]) -> None:
    """Print a one-line summary of what will be used."""
    mc_str = env.get("MODELS_COUNT", "0")
    try:
        models_count = int(mc_str) if mc_str else 0
    except ValueError:
        models_count = 0

    if models_count > 0:
        models = []
        for n in range(1, models_count + 1):
            mid = env.get(f"MODEL_{n}_ID", "")
            if mid:
                models.append(f"{mid}")
        print(
            f"Config: {models_count} model(s) — {', '.join(models)} "
            f"max_len={env.get('MAX_MODEL_LEN', '8192')} "
            f"gpu_util={env.get('GPU_MEMORY_UTILIZATION', '0.9')} "
            f"gpu_count={env.get('GPU_COUNT', '1')}",
            file=sys.stderr,
        )
    else:
        print(
            f"Config: model={env.get('MODEL_ID', '?')} "
            f"source={env.get('MODEL_SOURCE', '?')} "
            f"max_len={env.get('MAX_MODEL_LEN', '8192')} "
            f"gpu_util={env.get('GPU_MEMORY_UTILIZATION', '0.9')} "
            f"gpu_count={env.get('GPU_COUNT', '1')}",
            file=sys.stderr,
        )


# ── Public API (for unit tests) ─────────────────────────────────────────────


def _render_litellm_config(env: dict[str, str]) -> str:
    """Generate a rendered LiteLLM config.yaml with real model names.

    LiteLLM does NOT perform shell-style ${VAR} substitution in its config
    file at runtime — that syntax is interpreted literally, causing every
    request to fail with "model not found".  This function produces a fully
    resolved YAML config where all model names are concrete strings.

    The master_key is kept as ``os.environ/LITELLM_MASTER_KEY`` (LiteLLM's
    native env-var reference syntax) so that no secrets ever appear in the
    rendered file on disk.

    Returns a valid YAML string ready for LiteLLM to consume.
    """
    mc_str = env.get("MODELS_COUNT", "0")
    try:
        models_count = int(mc_str) if mc_str else 0
    except ValueError:
        models_count = 0

    model_list: list[dict] = []

    if models_count > 0:
        # Multi-model mode — one entry per MODEL_N_ID
        for n in range(1, models_count + 1):
            model_id = env.get(f"MODEL_{n}_ID", "").strip()
            if not model_id:
                continue
            model_list.append({
                "model_name": model_id,
                "litellm_params": {
                    "model": f"openai/{model_id}",
                    "api_base": "http://ray-head:8000/v1",
                    "api_key": "no-auth-internal",
                },
            })
    else:
        # Single-model mode — use MODEL_ID
        model_id = env.get("MODEL_ID", "").strip()
        if model_id:
            model_list.append({
                "model_name": model_id,
                "litellm_params": {
                    "model": f"openai/{model_id}",
                    "api_base": "http://ray-head:8000/v1",
                    "api_key": "no-auth-internal",
                },
            })

    config: dict = {
        "model_list": model_list,
        "general_settings": {
            # master_key as LiteLLM env-var reference — resolved at container startup.
            # Never substitute the real value here: this file is written to disk.
            "master_key": _LITELLM_MASTER_KEY_REF,
            "max_parallel_requests": 20,
        },
        "litellm_settings": {
            # LiteLLM 1.84.0+ requires auth on /metrics by default (PR #24600).
            # Opt out so Prometheus can scrape without a bearer token.
            "require_auth_for_metrics_endpoint": False,
            "default_team_settings": [
                {"team_id": "hard",    "rpm_limit": 15, "tpm_limit": 50_000},
                {"team_id": "regular", "rpm_limit": 4,  "tpm_limit": 15_000},
                {"team_id": "light",   "rpm_limit": 1,  "tpm_limit":  5_000},
            ],
        },
    }

    return yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _write_rendered_files(
    rendered_serve: str,
    rendered_litellm: str,
    repo_root: "Path",
) -> tuple["Path", "Path"]:
    """Write both rendered configs to *repo_root*.

    Returns (serve_path, litellm_path).
    Raises SystemExit on I/O failure.
    """
    serve_out = repo_root / "rendered_serve_config.yaml"
    litellm_out = repo_root / LITELLM_RENDERED_FILENAME
    try:
        serve_out.write_text(rendered_serve, encoding="utf-8")
        litellm_out.write_text(rendered_litellm, encoding="utf-8")
    except OSError as exc:
        print(
            f"FATAL: Could not write rendered configs to {repo_root}: {exc}\n"
            f"  Check that the directory exists and is writable.",
            file=sys.stderr,
        )
        sys.exit(1)
    return serve_out, litellm_out


def render(
    template: str,
    overrides: dict[str, str] | None = None,
) -> str:
    """Render a serve_config template string with env vars, returning YAML string.

    Pure function — no IO. Used for unit tests.
    """
    env = dict(os.environ)
    if overrides:
        env.update(overrides)

    _apply_defaults(env)
    _validate_schema_values(env)
    rendered = _substitute(template, env)
    _validate_yaml(rendered)
    return rendered


def render_litellm_config(
    overrides: dict[str, str] | None = None,
) -> str:
    """Render LiteLLM config from env + optional overrides.

    Pure function — no IO. Used for unit tests.
    """
    env = dict(os.environ)
    if overrides:
        env.update(overrides)
    _apply_defaults(env)
    _validate_schema_values(env)
    return _render_litellm_config(env)


def render_file(template_path: str | Path) -> tuple[str, dict[str, str]]:
    """Read a template file, render it, return (rendered_yaml, env_used)."""
    path = Path(template_path)
    raw = _read_file(path)
    env = _collect_env()
    rendered = _substitute(raw, env)
    _validate_yaml(rendered)
    _log_diagnostics(env)
    return rendered, env


# ── CLI entrypoint ──────────────────────────────────────────────────────────


def main() -> None:
    """Entry point for the Docker CMD and CLI operations.

    Steps (normal mode — Docker CMD):
        1. Locate and read template.
        2. Collect environment.
        3. Substitute and validate.
        4. Write rendered output to deterministic path.
        5. exec serve run.

    Flags:
        --dry-run      Render serve_config to stdout only. No files written,
                       no Ray Serve launched. Useful for config validation.
        --render-all   Render BOTH configs (serve + litellm) to repo root.
                       Used by ./idia deploy local before docker compose up.
    """
    caller_dir = Path(__file__).resolve().parent
    repo_root = caller_dir.parent  # scripts/../ = repo root

    # ── --render-all: write both configs to repo root, then exit ──────────
    if "--render-all" in sys.argv:
        env = _collect_env()
        raw = _read_file(_find_template(caller_dir))
        rendered_serve = _substitute(raw, env)
        _validate_yaml(rendered_serve)
        rendered_litellm = _render_litellm_config(env)
        serve_out, litellm_out = _write_rendered_files(rendered_serve, rendered_litellm, repo_root)
        _log_diagnostics(env)
        print(f"  serve config  → {serve_out}", file=sys.stderr)
        print(f"  litellm config → {litellm_out}", file=sys.stderr)
        return

    # ── --dry-run: render serve_config to stdout only ──
    if "--dry-run" in sys.argv:
        env = _collect_env()
        raw = _read_file(_find_template(caller_dir))
        rendered = _substitute(raw, env)
        _validate_yaml(rendered)
        print(rendered)
        return

    # ── Normal mode: render + write + exec serve run (Docker CMD) ─────────
    template_path = _find_template(caller_dir)
    raw = _read_file(template_path)
    env = _collect_env()
    rendered = _substitute(raw, env)
    _validate_yaml(rendered)
    _log_diagnostics(env)

    # Write rendered output to a deterministic path (overwrites on each run)
    RENDERED_PATH.write_text(rendered, encoding="utf-8")
    print(f"Rendered → {RENDERED_PATH}", file=sys.stderr)

    # Launch Ray Serve — replaces this process
    os.execlp("serve", "serve", "run", str(RENDERED_PATH))
    # Only reached on execlp failure
    print("FATAL: execlp failed to launch serve", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
