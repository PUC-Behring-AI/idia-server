"""Tests for per-model engine configuration in scripts/render_config.py.

Covers the three knobs an operator needs to serve a real model — dtype,
quantization and residency — plus the tool-calling flag that every modern
client depends on (ADR-011).

The recurring defect these guard against is not a wrong value: it is a value
that was *accepted and ignored*. Declaring MODEL_1_QUANTIZATION=awq in
single-model mode used to render a config with no quantization line at all,
and nothing said so.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def render_with(repo_root: Path, **env: str) -> dict:
    """Render serve_config.yaml under a clean environment; return parsed YAML."""
    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "render_config.py"), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", **env},
    )
    assert result.returncode == 0, f"render failed: {result.stderr}"
    return yaml.safe_load(result.stdout)


def engine_kwargs(parsed: dict, index: int = 0) -> dict:
    return parsed["applications"][0]["args"]["llm_configs"][index]["engine_kwargs"]


def autoscaling(parsed: dict, index: int = 0) -> dict:
    cfg = parsed["applications"][0]["args"]["llm_configs"][index]
    return cfg["deployment_config"]["autoscaling_config"]


@pytest.fixture(autouse=True)
def _scripts_importable(repo_root: Path):
    """Make `from scripts.render_config import ...` work from the repo root."""
    sys.path.insert(0, str(repo_root))
    yield
    sys.path.pop(0)


SINGLE = {"MODEL_ID": "mistral-7b", "MODEL_SOURCE": "mistralai/Mistral-7B-Instruct-v0.3"}


# ── Defaults ────────────────────────────────────────────────────────────


class TestDefaults:
    def test_dtype_defaults_to_bfloat16(self, repo_root: Path) -> None:
        assert engine_kwargs(render_with(repo_root, **SINGLE))["dtype"] == "bfloat16"

    def test_no_quantization_line_when_unset(self, repo_root: Path) -> None:
        assert "quantization" not in engine_kwargs(render_with(repo_root, **SINGLE))

    def test_min_replicas_defaults_to_scale_to_zero(self, repo_root: Path) -> None:
        assert autoscaling(render_with(repo_root, **SINGLE))["min_replicas"] == 0


# ── Tool calling (ADR-011) ──────────────────────────────────────────────


class TestToolCalling:
    """Every generated entry must enable auto tool choice, and none may set a parser."""

    def test_enabled_in_single_model(self, repo_root: Path) -> None:
        assert engine_kwargs(render_with(repo_root, **SINGLE))["enable_auto_tool_choice"] is True

    def test_enabled_in_multi_model(self, repo_root: Path) -> None:
        parsed = render_with(
            repo_root,
            MODELS_COUNT="2",
            GPU_COUNT="2",
            MODEL_1_ID="a", MODEL_1_SOURCE="org/a",
            MODEL_2_ID="b", MODEL_2_SOURCE="org/b",
        )
        for i in (0, 1):
            assert engine_kwargs(parsed, i)["enable_auto_tool_choice"] is True

    def test_no_tool_call_parser(self, repo_root: Path) -> None:
        """A parser overrode Qwen3's native template and killed the GPU worker."""
        assert "tool_call_parser" not in engine_kwargs(render_with(repo_root, **SINGLE))


# ── Per-model overrides ─────────────────────────────────────────────────


class TestSingleModelOverrides:
    """MODEL_1_* must apply in single-model mode — this is the issue #7 defect."""

    def test_awq_config_is_honoured(self, repo_root: Path) -> None:
        parsed = render_with(
            repo_root,
            MODEL_ID="qwen3-8b",
            MODEL_SOURCE="Qwen/Qwen3-8B-AWQ",
            MODEL_1_QUANTIZATION="awq",
            MODEL_1_DTYPE="float16",
            MODEL_1_MIN_REPLICAS="1",
        )
        kwargs = engine_kwargs(parsed)
        assert kwargs["quantization"] == "awq"
        assert kwargs["dtype"] == "float16"
        assert autoscaling(parsed)["min_replicas"] == 1

    def test_unnumbered_form_also_applies(self, repo_root: Path) -> None:
        parsed = render_with(
            repo_root, **SINGLE, MODEL_DTYPE="float16", MODEL_QUANTIZATION="gptq"
        )
        kwargs = engine_kwargs(parsed)
        assert kwargs["dtype"] == "float16"
        assert kwargs["quantization"] == "gptq"

    def test_numbered_form_wins_over_unnumbered(self, repo_root: Path) -> None:
        parsed = render_with(
            repo_root, **SINGLE, MODEL_DTYPE="bfloat16", MODEL_1_DTYPE="float16"
        )
        assert engine_kwargs(parsed)["dtype"] == "float16"


class TestMultiModelOverrides:
    def test_each_model_keeps_its_own_settings(self, repo_root: Path) -> None:
        parsed = render_with(
            repo_root,
            MODELS_COUNT="2",
            GPU_COUNT="2",
            MODEL_1_ID="plain", MODEL_1_SOURCE="org/plain",
            MODEL_2_ID="quant", MODEL_2_SOURCE="org/quant-AWQ",
            MODEL_2_QUANTIZATION="awq", MODEL_2_DTYPE="float16",
            MODEL_2_MIN_REPLICAS="1",
        )
        first, second = engine_kwargs(parsed, 0), engine_kwargs(parsed, 1)
        assert first["dtype"] == "bfloat16"
        assert "quantization" not in first
        assert autoscaling(parsed, 0)["min_replicas"] == 0

        assert second["dtype"] == "float16"
        assert second["quantization"] == "awq"
        assert autoscaling(parsed, 1)["min_replicas"] == 1


# ── Failure modes that used to be silent ────────────────────────────────


class TestNoSilentDrops:
    def test_incomplete_multi_model_entry_is_fatal(self, repo_root: Path) -> None:
        """MODELS_COUNT=2 with one model defined must fail, not render one entry."""
        result = subprocess.run(
            [sys.executable, str(repo_root / "scripts" / "render_config.py"), "--dry-run"],
            capture_output=True, text=True, timeout=30,
            env={
                "PATH": "/usr/bin:/bin",
                "MODELS_COUNT": "2",
                "GPU_COUNT": "2",
                "MODEL_1_ID": "a", "MODEL_1_SOURCE": "org/a",
                "MODEL_2_ID": "b",  # MODEL_2_SOURCE deliberately missing
            },
        )
        assert result.returncode != 0
        assert "MODEL_2_SOURCE" in result.stderr

    def test_single_model_without_source_is_fatal(self, repo_root: Path) -> None:
        result = subprocess.run(
            [sys.executable, str(repo_root / "scripts" / "render_config.py"), "--dry-run"],
            capture_output=True, text=True, timeout=30,
            env={"PATH": "/usr/bin:/bin", "MODEL_ID": "orphan"},
        )
        assert result.returncode != 0


# ── Single source of truth for the entry shape ──────────────────────────


class TestNoStaticFallback:
    """serve_config.yaml must not carry a second, drifting copy of the entry."""

    def test_template_has_no_active_llm_config_entry(self, repo_root: Path) -> None:
        raw = (repo_root / "serve_config.yaml").read_text(encoding="utf-8")
        active = [
            line for line in raw.splitlines()
            if line.strip().startswith("- model_loading_config:")
            and not line.lstrip().startswith("#")
        ]
        assert not active, f"static entry still present: {active}"

    def test_marker_is_present(self, repo_root: Path) -> None:
        raw = (repo_root / "serve_config.yaml").read_text(encoding="utf-8")
        marker_lines = [
            line for line in raw.splitlines()
            if "##LLM_CONFIGS##" in line and not line.lstrip().startswith("#")
        ]
        assert len(marker_lines) == 1, "exactly one active marker expected"
        assert marker_lines[0].lstrip().startswith("llm_configs:")

    def test_marker_inside_a_comment_is_ignored(self) -> None:
        """A comment that names the marker must not splice YAML into the file.

        This bit during development: the header comment mentioned the marker
        by name, the substitution fired there, and the rendered file grew a
        second YAML document.
        """
        from scripts.render_config import render

        template = (
            "# um comentário que cita ##LLM_CONFIGS## por nome\n"
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
        )
        parsed = yaml.safe_load(render(template, overrides=SINGLE))
        assert len(parsed["applications"][0]["args"]["llm_configs"]) == 1

