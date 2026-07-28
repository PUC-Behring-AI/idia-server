"""Integration tests for the IDIA Server build core.

These tests validate the integration between components without requiring
a real Docker or GPU environment. They exercise:

    - render_config.py: env var substitution, YAML output, error paths
    - serve_config.yaml: structural integrity after rendering
    - docker-compose.yml: service dependency graph
    - config.yaml: LiteLLM routing consistency with serve_config

Tests that genuinely require a running container (docker compose up,
E2E inference, GPU detection) are marked @pytest.mark.integration and
skip when Docker is unavailable — they target pre-release validation on
GPU-equipped hardware and are documented for manual execution.

See docs/ARCHITECTURE.md §11 for the testing philosophy.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def scripts_dir(repo_root: Path) -> Path:
    return repo_root / "scripts"


@pytest.fixture
def serve_config_yaml(repo_root: Path) -> Path:
    return repo_root / "serve_config.yaml"


# ── render_config.py integration tests ─────────────────────────────────────


@pytest.mark.integration
class TestRenderConfig:
    """Exercise ``scripts/render_config.py`` as a module.

    These tests call ``render_config.render()`` directly — a pure function
    that substitutes env var placeholders and validates the YAML output.
    No Docker or GPU required.
    """

    def test_render_with_minimal_env(self, scripts_dir: Path) -> None:
        """Minimal required env vars produce valid rendered YAML."""
        # render needs to be importable from the scripts dir
        sys.path.insert(0, str(scripts_dir.parent))
        try:
            from scripts.render_config import render  # type: ignore[import-untyped]
        finally:
            sys.path.pop(0)

        overrides = {
            "MODEL_ID": "test-model",
            "MODEL_SOURCE": "test-org/test-model",
        }
        rendered = render(
            yaml.safe_dump({
                "proxy_location": "EveryNode",
                "http_options": {"host": "0.0.0.0", "port": 8000},
                "applications": [{
                    "name": "llms",
                    "import_path": "ray.serve.llm:build_openai_app",
                    "route_prefix": "/",
                    "args": {
                        "llm_configs": [{
                            "model_loading_config": {
                                "model_id": "${MODEL_ID}",
                                "model_source": "${MODEL_SOURCE}",
                            },
                            "deployment_config": {
                                "autoscaling_config": {
                                    "min_replicas": 0,
                                    "max_replicas": 4,
                                    "target_ongoing_requests": 64,
                                },
                            },
                        }],
                    },
                }],
            }),
            overrides=overrides,
        )
        parsed = yaml.safe_load(rendered)
        assert parsed["applications"][0]["args"]["llm_configs"][0]["model_loading_config"]["model_id"] == "test-model"
        assert parsed["applications"][0]["args"]["llm_configs"][0]["model_loading_config"]["model_source"] == "test-org/test-model"

    def test_render_injects_defaults(self, scripts_dir: Path) -> None:
        """Optional vars get defaults injected when absent from env."""
        sys.path.insert(0, str(scripts_dir.parent))
        try:
            from scripts.render_config import render
        finally:
            sys.path.pop(0)

        overrides: dict[str, str] = {
            "MODEL_ID": "test-model",
            "MODEL_SOURCE": "test-org/test-model",
            "MAX_MODEL_LEN": "4096",  # override default
            # GPU_MEMORY_UTILIZATION NOT set — should use default 0.9
        }
        rendered = render(
            yaml.safe_dump({
                "proxy_location": "EveryNode",
                "http_options": {"host": "0.0.0.0", "port": 8000},
                "applications": [{
                    "name": "llms",
                    "import_path": "ray.serve.llm:build_openai_app",
                    "route_prefix": "/",
                    "args": {
                        "llm_configs": [{
                            "model_loading_config": {
                                "model_id": "${MODEL_ID}",
                                "model_source": "${MODEL_SOURCE}",
                            },
                            "engine_kwargs": {
                                "gpu_memory_utilization": "${GPU_MEMORY_UTILIZATION}",
                                "max_model_len": "${MAX_MODEL_LEN}",
                            },
                            "deployment_config": {
                                "autoscaling_config": {
                                    "min_replicas": 0,
                                    "max_replicas": 4,
                                    "target_ongoing_requests": 64,
                                },
                            },
                        }],
                    },
                }],
            }),
            overrides=overrides,
        )
        parsed = yaml.safe_load(rendered)
        engine = parsed["applications"][0]["args"]["llm_configs"][0]["engine_kwargs"]
        assert isinstance(engine["gpu_memory_utilization"], float), (
            f"Expected float, got {type(engine['gpu_memory_utilization'])}"
        )
        assert engine["gpu_memory_utilization"] == 0.9
        assert isinstance(engine["max_model_len"], int), (
            f"Expected int, got {type(engine['max_model_len'])}"
        )
        assert engine["max_model_len"] == 4096

    def test_render_validates_full_template(self, serve_config_yaml: Path) -> None:
        """The real serve_config.yaml template renders to valid YAML."""
        sys.path.insert(0, str(serve_config_yaml.parent))
        try:
            from scripts.render_config import render
        finally:
            sys.path.pop(0)

        template = serve_config_yaml.read_text(encoding="utf-8")
        overrides = {
            "MODEL_ID": "test-model",
            "MODEL_SOURCE": "test-org/test-model",
            "MAX_MODEL_LEN": "4096",
            "GPU_MEMORY_UTILIZATION": "0.85",
        }
        rendered = render(template, overrides=overrides)
        parsed = yaml.safe_load(rendered)

        # Structural assertions matching ARCHITECTURE.md §5.3
        assert parsed["proxy_location"] == "EveryNode"
        assert parsed["http_options"]["port"] == 8000
        apps = parsed["applications"]
        assert len(apps) == 1
        llm_cfg = apps[0]["args"]["llm_configs"][0]
        assert llm_cfg["model_loading_config"]["model_id"] == "test-model"
        assert llm_cfg["deployment_config"]["autoscaling_config"]["min_replicas"] == 0
        assert llm_cfg["deployment_config"]["autoscaling_config"]["max_replicas"] == 4
        # Health check fields (T4.3 / STRUCT-14)
        assert llm_cfg["deployment_config"]["health_check_period_s"] == 30
        assert llm_cfg["deployment_config"]["health_check_timeout_s"] == 10

    def test_multi_model_renders_multiple_entries(self) -> None:
        """Multi-model MODELS_COUNT=N generates N llm_config entries."""
        from scripts.render_config import render

        template = (
            "proxy_location: EveryNode\n"
            "http_options:\n"
            "  host: 0.0.0.0\n"
            "  port: 8000\n"
            "applications:\n"
            "  - name: llms\n"
            "    import_path: ray.serve.llm:build_openai_app\n"
            "    route_prefix: /\n"
            "    args:\n"
            "      llm_configs: ##LLM_CONFIGS##\n"
            "        - model_loading_config:\n"
            "            model_id: ${MODEL_ID}\n"
            "            model_source: ${MODEL_SOURCE}\n"
        )
        overrides = {
            "MODELS_COUNT": "2",
            "MODEL_1_ID": "llama-3.1-8b",
            "MODEL_1_SOURCE": "meta-llama/Llama-3.1-8B-Instruct",
            "MODEL_2_ID": "qwen-2.5-14b",
            "MODEL_2_SOURCE": "Qwen/Qwen2.5-14B-Instruct",
            "MODEL_ID": "fallback",
            "MODEL_SOURCE": "org/fallback",
            "GPU_COUNT": "2",
        }
        rendered = render(template, overrides=overrides)
        # Marker should NOT appear in output
        assert "LLM_CONFIGS" not in rendered, f"Marker leaked:\n{rendered}"
        parsed = yaml.safe_load(rendered)
        configs = parsed["applications"][0]["args"]["llm_configs"]
        assert len(configs) == 2, f"Expected 2 llm_configs, got {len(configs)}"
        assert configs[0]["model_loading_config"]["model_id"] == "llama-3.1-8b"
        assert configs[1]["model_loading_config"]["model_id"] == "qwen-2.5-14b"
        # Fallback entry should NOT appear (multi-model mode removes it)
        assert not any(c["model_loading_config"]["model_id"] == "fallback" for c in configs)

    def test_multi_model_single_model_backward_compat(self) -> None:
        """Single MODEL_ID works when MODELS_COUNT is absent (marker removed, fallback kept)."""
        from scripts.render_config import render

        template = (
            "proxy_location: EveryNode\n"
            "http_options:\n"
            "  host: 0.0.0.0\n"
            "  port: 8000\n"
            "applications:\n"
            "  - name: llms\n"
            "    import_path: ray.serve.llm:build_openai_app\n"
            "    route_prefix: /\n"
            "    args:\n"
            "      llm_configs: ##LLM_CONFIGS##\n"
            "        - model_loading_config:\n"
            "            model_id: ${MODEL_ID}\n"
            "            model_source: ${MODEL_SOURCE}\n"
        )
        overrides = {
            "MODEL_ID": "test-model",
            "MODEL_SOURCE": "test-org/test-model",
        }
        rendered = render(template, overrides=overrides)
        assert "LLM_CONFIGS" not in rendered, f"Marker leaked:\n{rendered}"
        parsed = yaml.safe_load(rendered)
        configs = parsed["applications"][0]["args"]["llm_configs"]
        assert len(configs) == 1, f"Expected 1 llm_config, got {len(configs)}"
        assert configs[0]["model_loading_config"]["model_id"] == "test-model"

    def test_dry_run_flag(self, scripts_dir: Path) -> None:
        """--dry-run renders to stdout without launching serve."""
        env = os.environ.copy()
        env.update(
            MODEL_ID="test-model",
            MODEL_SOURCE="test-org/test-model",
        )
        result = subprocess.run(
            [sys.executable, str(scripts_dir / "render_config.py"), "--dry-run"],
            capture_output=True,
            text=True,
            env=env,
            cwd=scripts_dir,
        )
        assert result.returncode == 0, f"dry-run failed: {result.stderr}"
        # Output should be valid YAML
        parsed = yaml.safe_load(result.stdout)
        assert parsed is not None
        assert "applications" in parsed


@pytest.mark.integration
class TestRenderConfigErrors:
    """Error paths in render_config.py."""

    def test_missing_required_var_fails(self, scripts_dir: Path) -> None:
        """Missing MODEL_ID exits with code 1."""
        env = os.environ.copy()
        env.pop("MODEL_ID", None)
        env["MODEL_SOURCE"] = "test-org/test-model"
        result = subprocess.run(
            [sys.executable, str(scripts_dir / "render_config.py"), "--dry-run"],
            capture_output=True,
            text=True,
            env=env,
            cwd=scripts_dir,
        )
        assert result.returncode != 0
        assert "MODEL_ID" in result.stderr

    def test_bad_yaml_template_fails(self, scripts_dir: Path) -> None:
        """A template producing invalid YAML after substitution exits with 1."""
        sys.path.insert(0, str(scripts_dir.parent))
        try:
            from scripts.render_config import render
        finally:
            sys.path.pop(0)

        # Inject an unclosed mapping after substitution
        with pytest.raises(SystemExit):
            render("key: ${UNKNOWN_VAR}\n  bad_indent", overrides={})


@pytest.mark.integration
class TestRenderSchemaErrors:
    """Schema and input validation in render_config.py."""

    def test_gpu_util_above_range_fails(self, scripts_dir: Path) -> None:
        """GPU_MEMORY_UTILIZATION > 1.0 raises SystemExit."""
        sys.path.insert(0, str(scripts_dir.parent))
        try:
            from scripts.render_config import render
        finally:
            sys.path.pop(0)

        overrides = {
            "MODEL_ID": "test-model",
            "MODEL_SOURCE": "test-org/test-model",
            "GPU_MEMORY_UTILIZATION": "1.5",
        }
        with pytest.raises(SystemExit):
            render("model_id: ${MODEL_ID}", overrides=overrides)

    def test_gpu_util_negative_fails(self, scripts_dir: Path) -> None:
        """GPU_MEMORY_UTILIZATION <= 0 raises SystemExit."""
        sys.path.insert(0, str(scripts_dir.parent))
        try:
            from scripts.render_config import render
        finally:
            sys.path.pop(0)

        overrides = {
            "MODEL_ID": "test-model",
            "MODEL_SOURCE": "test-org/test-model",
            "GPU_MEMORY_UTILIZATION": "-0.5",
        }
        with pytest.raises(SystemExit):
            render("model_id: ${MODEL_ID}", overrides=overrides)

    def test_gpu_count_zero_fails(self, scripts_dir: Path) -> None:
        """GPU_COUNT=0 raises SystemExit."""
        sys.path.insert(0, str(scripts_dir.parent))
        try:
            from scripts.render_config import render
        finally:
            sys.path.pop(0)

        overrides = {
            "MODEL_ID": "test-model",
            "MODEL_SOURCE": "test-org/test-model",
            "GPU_COUNT": "0",
        }
        with pytest.raises(SystemExit):
            render("model_id: ${MODEL_ID}", overrides=overrides)

    def test_gpu_vram_gb_zero_fails(self, scripts_dir: Path) -> None:
        """GPU_VRAM_GB=0 raises SystemExit."""
        sys.path.insert(0, str(scripts_dir.parent))
        try:
            from scripts.render_config import render
        finally:
            sys.path.pop(0)

        overrides = {
            "MODEL_ID": "test-model",
            "MODEL_SOURCE": "test-org/test-model",
            "GPU_VRAM_GB": "0",
        }
        with pytest.raises(SystemExit):
            render("model_id: ${MODEL_ID}", overrides=overrides)

    def test_max_model_len_non_numeric_fails(self, scripts_dir: Path) -> None:
        """MAX_MODEL_LEN=abc raises SystemExit."""
        sys.path.insert(0, str(scripts_dir.parent))
        try:
            from scripts.render_config import render
        finally:
            sys.path.pop(0)

        overrides = {
            "MODEL_ID": "test-model",
            "MODEL_SOURCE": "test-org/test-model",
            "MAX_MODEL_LEN": "abc",
        }
        with pytest.raises(SystemExit):
            render("model_id: ${MODEL_ID}", overrides=overrides)

    def test_multi_model_vram_budget_exceeds_gpu_count(self, scripts_dir: Path) -> None:
        """3 models on 1 GPU fails VRAM budget check."""
        sys.path.insert(0, str(scripts_dir.parent))
        try:
            from scripts.render_config import render
        finally:
            sys.path.pop(0)

        overrides = {
            "MODELS_COUNT": "3",
            "MODEL_1_ID": "model-a", "MODEL_1_SOURCE": "org/a",
            "MODEL_2_ID": "model-b", "MODEL_2_SOURCE": "org/b",
            "MODEL_3_ID": "model-c", "MODEL_3_SOURCE": "org/c",
            "GPU_COUNT": "1",
            "GPU_MEMORY_UTILIZATION": "0.9",
        }
        template = (
            "proxy_location: EveryNode\n"
            "http_options:\n"
            "  host: 0.0.0.0\n"
            "  port: 8000\n"
            "applications:\n"
            "  - name: llms\n"
            "    import_path: ray.serve.llm:build_openai_app\n"
            "    route_prefix: /\n"
            "    args:\n"
            "      llm_configs:\n"
            "        - model_loading_config:\n"
            "            model_id: ${MODEL_1_ID}\n"
            "            model_source: ${MODEL_1_SOURCE}\n"
        )
        with pytest.raises(SystemExit):
            render(template, overrides=overrides)

    def test_multi_model_vram_budget_fits_gpu_count(self, scripts_dir: Path) -> None:
        """2 models on 2 GPUs passes VRAM budget check."""
        sys.path.insert(0, str(scripts_dir.parent))
        try:
            from scripts.render_config import render
        finally:
            sys.path.pop(0)

        overrides = {
            "MODELS_COUNT": "2",
            "MODEL_1_ID": "model-a", "MODEL_1_SOURCE": "org/a",
            "MODEL_2_ID": "model-b", "MODEL_2_SOURCE": "org/b",
            "GPU_COUNT": "2",
            "GPU_MEMORY_UTILIZATION": "0.9",
        }
        # Use a minimal valid template so _validate_yaml passes
        template = (
            "proxy_location: EveryNode\n"
            "http_options:\n"
            "  host: 0.0.0.0\n"
            "  port: 8000\n"
            "applications:\n"
            "  - name: llms\n"
            "    import_path: ray.serve.llm:build_openai_app\n"
            "    route_prefix: /\n"
            "    args:\n"
            "      llm_configs:\n"
            "        - model_loading_config:\n"
            "            model_id: ${MODEL_1_ID}\n"
            "            model_source: ${MODEL_1_SOURCE}\n"
        )
        # Should pass without SystemExit
        render(template, overrides=overrides)

    def test_model_id_with_yaml_special_chars_escaped(self, scripts_dir: Path) -> None:
        """MODEL_ID with YAML special chars is escaped, not injected."""
        sys.path.insert(0, str(scripts_dir.parent))
        try:
            from scripts.render_config import render
        finally:
            sys.path.pop(0)

        overrides = {
            "MODEL_ID": 'test: {evil: true}',
            "MODEL_SOURCE": "test-org/test-model",
        }
        rendered = render(
            yaml.safe_dump({
                "proxy_location": "EveryNode",
                "http_options": {"host": "0.0.0.0", "port": 8000},
                "applications": [{
                    "name": "llms",
                    "import_path": "ray.serve.llm:build_openai_app",
                    "route_prefix": "/",
                    "args": {
                        "llm_configs": [{
                            "model_loading_config": {
                                "model_id": "${MODEL_ID}",
                                "model_source": "${MODEL_SOURCE}",
                            },
                            "deployment_config": {
                                "autoscaling_config": {
                                    "min_replicas": 0,
                                    "max_replicas": 1,
                                    "target_ongoing_requests": 64,
                                },
                            },
                        }],
                    },
                }],
            }),
            overrides=overrides,
        )
        parsed = yaml.safe_load(rendered)
        # The entire value should be kept as a single string
        mlc = parsed["applications"][0]["args"]["llm_configs"][0]["model_loading_config"]
        assert mlc["model_id"] == 'test: {evil: true}'
        assert mlc["model_source"] == "test-org/test-model"


# ── Compose dependency consistency ──────────────────────────────────────────


@pytest.mark.integration
class TestComposeConsistency:
    """Validate the docker-compose.yml structure.

    These tests run without Docker — they parse the YAML and check
    structural properties. Actual compose validation requires a host
    with Docker engine.
    """

    def test_ray_head_builds_locally(self, repo_root: Path) -> None:
        """ray-head service builds from local Dockerfile, not an image."""
        compose = yaml.safe_load(
            (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
        )
        ray = compose["services"]["ray-head"]
        assert "build" in ray, "ray-head must build from local Dockerfile"
        assert ray["build"]["dockerfile"] == "Dockerfile.ray"

    def test_litellm_uses_pinned_image(self, repo_root: Path) -> None:
        """LiteLLM image tag is a semver, not :latest."""
        compose = yaml.safe_load(
            (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
        )
        image = compose["services"]["litellm"]["image"]
        assert ":latest" not in image, f"LiteLLM image uses :latest: {image}"
        assert re.search(r":v?\d+\.\d+\.\d+", image), f"LiteLLM image not pinned: {image}"

    def test_ray_head_passes_vars_to_entrypoint(self, repo_root: Path) -> None:
        """ray-head passes all env vars required by render_config.py."""
        compose = yaml.safe_load(
            (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
        )
        env_list = compose["services"]["ray-head"].get("environment", [])
        env_str = "\n".join(env_list) if isinstance(env_list, list) else str(env_list)
        for var in ["MODEL_ID", "MODEL_SOURCE", "MAX_MODEL_LEN", "GPU_MEMORY_UTILIZATION"]:
            assert var in env_str, f"ray-head missing env var: {var}"

    def test_litellm_mounts_rendered_config(self, repo_root: Path) -> None:
        """litellm mounts rendered_litellm_config.yaml, not config.yaml directly.

        LiteLLM does not perform ${VAR} substitution in its config file.
        Mounting the raw config.yaml would result in all model names being
        literal strings like '${MODEL_ID:-llama-3.1-8b}', causing 404s.
        The rendered config is generated by `./idia deploy local` before
        `docker compose up`. See docs/ARCHITECTURE.md §4.3.
        """
        compose = yaml.safe_load(
            (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
        )
        volumes = compose["services"]["litellm"].get("volumes", [])
        volume_str = "\n".join(str(v) for v in volumes)
        assert "rendered_litellm_config.yaml" in volume_str, (
            "litellm must mount rendered_litellm_config.yaml (not raw config.yaml). "
            "LiteLLM does not substitute ${VAR} in its config — raw config causes model not found."
        )
        # The raw config.yaml must NOT be mounted (would break model routing)
        assert "./config.yaml:/app/config.yaml" not in volume_str, (
            "litellm must not mount raw config.yaml directly."
        )

    def test_rendered_litellm_not_committed(self, repo_root: Path) -> None:
        """rendered_litellm_config.yaml is in .gitignore."""
        gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8")
        assert "rendered_litellm_config.yaml" in gitignore, (
            "rendered_litellm_config.yaml must be in .gitignore — "
            "it contains real model names derived from .env."
        )


# ── LiteLLM config rendering ────────────────────────────────────────────────


@pytest.mark.integration
class TestRenderLiteLLMConfig:
    """Tests for render_config.render_litellm_config()."""

    def _import_render_litellm(self, scripts_dir: Path):
        sys.path.insert(0, str(scripts_dir.parent))
        try:
            from scripts.render_config import render_litellm_config
            return render_litellm_config
        finally:
            sys.path.pop(0)

    def test_single_model_produces_one_entry(self, scripts_dir: Path) -> None:
        """Single-model mode generates exactly 1 model_list entry."""
        render_litellm_config = self._import_render_litellm(scripts_dir)
        rendered = render_litellm_config(overrides={
            "MODEL_ID": "llama-3.1-8b",
            "MODEL_SOURCE": "meta-llama/Llama-3.1-8B-Instruct",
        })
        parsed = yaml.safe_load(rendered)
        assert len(parsed["model_list"]) == 1
        entry = parsed["model_list"][0]
        assert entry["model_name"] == "llama-3.1-8b"
        assert entry["litellm_params"]["model"] == "openai/llama-3.1-8b"
        assert entry["litellm_params"]["api_base"] == "http://ray-head:8000/v1"

    def test_multi_model_produces_n_entries(self, scripts_dir: Path) -> None:
        """Multi-model mode generates N model_list entries from MODEL_N_ID vars."""
        render_litellm_config = self._import_render_litellm(scripts_dir)
        rendered = render_litellm_config(overrides={
            "MODELS_COUNT": "2",
            "MODEL_1_ID": "llama-3.1-8b",
            "MODEL_1_SOURCE": "meta-llama/Llama-3.1-8B-Instruct",
            "MODEL_2_ID": "qwen-2.5-14b",
            "MODEL_2_SOURCE": "Qwen/Qwen2.5-14B-Instruct",
            "GPU_COUNT": "2",
        })
        parsed = yaml.safe_load(rendered)
        assert len(parsed["model_list"]) == 2
        names = [e["model_name"] for e in parsed["model_list"]]
        assert "llama-3.1-8b" in names
        assert "qwen-2.5-14b" in names

    def test_rate_limit_tiers_preserved(self, scripts_dir: Path) -> None:
        """All three rate-limit tiers (hard/regular/light) appear in the rendered config."""
        render_litellm_config = self._import_render_litellm(scripts_dir)
        rendered = render_litellm_config(overrides={
            "MODEL_ID": "test-model",
            "MODEL_SOURCE": "org/test-model",
        })
        parsed = yaml.safe_load(rendered)
        tiers = {
            t["team_id"]: t
            for t in parsed["litellm_settings"]["default_team_settings"]
        }
        assert "hard" in tiers,    "Missing 'hard' tier in rendered litellm config"
        assert "regular" in tiers, "Missing 'regular' tier in rendered litellm config"
        assert "light" in tiers,   "Missing 'light' tier in rendered litellm config"
        assert tiers["hard"]["rpm_limit"] == 15
        assert tiers["regular"]["rpm_limit"] == 4
        assert tiers["light"]["rpm_limit"] == 1

    def test_master_key_is_env_reference_not_hardcoded(self, scripts_dir: Path) -> None:
        """master_key uses os.environ/ syntax — the real value is never in the file."""
        render_litellm_config = self._import_render_litellm(scripts_dir)
        rendered = render_litellm_config(overrides={
            "MODEL_ID": "test-model",
            "MODEL_SOURCE": "org/test-model",
            "LITELLM_MASTER_KEY": "sk-real-secret-key",
        })
        # The actual secret must NOT appear in the rendered output
        assert "sk-real-secret-key" not in rendered, (
            "master_key must NOT be hardcoded in rendered litellm config"
        )
        # But the env-var reference must be present
        assert "os.environ/LITELLM_MASTER_KEY" in rendered, (
            "master_key must use LiteLLM os.environ/ reference syntax"
        )

    def test_max_parallel_requests_present(self, scripts_dir: Path) -> None:
        """max_parallel_requests is set in general_settings."""
        render_litellm_config = self._import_render_litellm(scripts_dir)
        rendered = render_litellm_config(overrides={
            "MODEL_ID": "test-model",
            "MODEL_SOURCE": "org/test-model",
        })
        parsed = yaml.safe_load(rendered)
        assert parsed["general_settings"]["max_parallel_requests"] == 20
