"""In-process unit tests for scripts/render_config.py.

The suite already exercised this module, but only through ``subprocess`` —
which coverage.py cannot see across a process boundary, and which cannot
reach a branch that fires on a ``PermissionError``, a corrupt template or a
failed ``execlp``. Every test here calls the function directly.

What they guard against is one class of defect: a failure that exits 0, or
exits 1 with a message that does not name the variable at fault. An operator
who reads ``FATAL`` without a variable name has to open the source to find
out what to change — which is the moment the error message stopped being an
error message.

The tests assert on *both* halves of every failure: the exit status and the
token in stderr that tells the operator where to look. Asserting only the
status passes just as well when the diagnostic goes blank.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from scripts.render_config import (  # noqa: E402
    ENV_SCHEMA,
    LITELLM_RENDERED_FILENAME,
    TEMPLATE_FILENAME,
    _apply_defaults,
    _build_llm_configs,
    _collect_env,
    _engine_option,
    _escape_yaml_value,
    _find_template,
    _log_diagnostics,
    _model_entry,
    _read_file,
    _render_litellm_config,
    _substitute,
    _validate_schema_values,
    _validate_yaml,
    _write_rendered_files,
    main,
    render,
    render_file,
    render_litellm_config,
)

SINGLE = {"MODEL_ID": "mistral-7b", "MODEL_SOURCE": "mistralai/Mistral-7B-Instruct-v0.3"}

# A minimal template carrying the marker on the llm_configs: key, which is the
# only position _substitute accepts.
TEMPLATE = """proxy_location: EveryNode
http_options:
  host: 0.0.0.0
  port: 8000
applications:
  - name: llms
    import_path: ray.serve.llm:build_openai_app
    route_prefix: /
    args:
      llm_configs: ##LLM_CONFIGS##
"""


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Replace ``os.environ`` with an empty dict the test controls.

    The module reads ``os.environ`` at call time, so swapping the attribute is
    enough. Without this, a developer who happens to export MODEL_ID in their
    shell gets different results from CI — the exact failure mode that makes a
    suite untrustworthy rather than merely red.
    """
    fake: dict[str, str] = {}
    monkeypatch.setattr(os, "environ", fake)
    return fake


class _ExecCalled(Exception):
    """Stands in for the process being replaced by ``os.execlp``."""


# ── _find_template ──────────────────────────────────────────────────────────


class TestFindTemplate:
    def test_finds_template_in_caller_dir(self, tmp_path: Path) -> None:
        (tmp_path / TEMPLATE_FILENAME).write_text(TEMPLATE, encoding="utf-8")
        assert _find_template(tmp_path) == tmp_path / TEMPLATE_FILENAME

    def test_finds_template_in_parent_dir(self, tmp_path: Path) -> None:
        """The Docker layout puts the script in scripts/ and the template above it."""
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (tmp_path / TEMPLATE_FILENAME).write_text(TEMPLATE, encoding="utf-8")
        assert _find_template(scripts) == tmp_path / TEMPLATE_FILENAME

    def test_finds_template_two_levels_up(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b"
        deep.mkdir(parents=True)
        (tmp_path / TEMPLATE_FILENAME).write_text(TEMPLATE, encoding="utf-8")
        assert _find_template(deep) == tmp_path / TEMPLATE_FILENAME

    def test_missing_template_is_fatal_and_lists_where_it_looked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The message must name the paths searched, not just say 'not found'.

        A bare 'not found' on a container start tells the operator nothing
        about which of the three candidate directories they mounted wrong.
        """
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(SystemExit) as exc:
            _find_template(empty)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert TEMPLATE_FILENAME in err
        assert str(empty) in err
        assert "/app" in err

    def test_falls_back_to_app_when_no_caller_dir(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With caller_dir=None the only candidate is the container path."""
        if Path("/app", TEMPLATE_FILENAME).is_file():  # pragma: no cover
            pytest.skip("running inside the container image, where /app has the template")
        with pytest.raises(SystemExit):
            _find_template(None)
        assert "/app" in capsys.readouterr().err


# ── _read_file ──────────────────────────────────────────────────────────────


class TestReadFile:
    def test_reads_utf8(self, tmp_path: Path) -> None:
        p = tmp_path / "t.yaml"
        p.write_text("olá: mundo\n", encoding="utf-8")
        assert _read_file(p) == "olá: mundo\n"

    def test_missing_file_is_fatal(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _read_file(tmp_path / "absent.yaml")
        assert exc.value.code == 1
        assert "não encontrado" in capsys.readouterr().err

    def test_unreadable_file_is_fatal(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        if os.geteuid() == 0:  # pragma: no cover
            pytest.skip("root ignores the permission bits this test relies on")
        p = tmp_path / "locked.yaml"
        p.write_text("a: 1\n", encoding="utf-8")
        p.chmod(0o000)
        try:
            with pytest.raises(SystemExit) as exc:
                _read_file(p)
        finally:
            p.chmod(0o600)
        assert exc.value.code == 1
        assert "Permissão negada" in capsys.readouterr().err

    def test_non_utf8_file_is_fatal_and_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A latin-1 template must fail naming the encoding, not raise a traceback.

        This is not hypothetical: a config edited on a Windows machine and
        committed with a cp1252 accent reaches the container as bytes Python
        refuses, and a traceback in a container log names the codec, never
        the fix.
        """
        p = tmp_path / "latin.yaml"
        p.write_bytes(b"modelo: caf\xe9\n")
        with pytest.raises(SystemExit) as exc:
            _read_file(p)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "encoding" in err
        assert "UTF-8" in err


# ── _apply_defaults / _engine_option / _escape_yaml_value ───────────────────


class TestApplyDefaults:
    def test_injects_optional_defaults_only(self) -> None:
        e: dict[str, str] = {}
        _apply_defaults(e)
        assert e["MAX_MODEL_LEN"] == "8192"
        assert e["GPU_MEMORY_UTILIZATION"] == "0.9"
        assert e["GPU_COUNT"] == "1"
        # Required vars carry default None and must stay absent.
        assert "MODEL_ID" not in e
        assert "MODEL_SOURCE" not in e
        assert "MODELS_COUNT" not in e

    def test_does_not_overwrite_an_explicit_value(self) -> None:
        e = {"MAX_MODEL_LEN": "4096"}
        _apply_defaults(e)
        assert e["MAX_MODEL_LEN"] == "4096"

    def test_every_required_var_is_marked_none(self) -> None:
        """Guards the schema itself: a required var with a default is not required."""
        required = {v for v, (_, d) in ENV_SCHEMA.items() if d is None}
        assert required == {"MODEL_ID", "MODEL_SOURCE", "MODELS_COUNT"}


class TestEngineOption:
    def test_numbered_wins(self) -> None:
        e = {"MODEL_1_DTYPE": "float16", "MODEL_DTYPE": "bfloat16"}
        assert _engine_option(e, 1, "DTYPE", "x") == "float16"

    def test_unnumbered_is_the_fallback(self) -> None:
        assert _engine_option({"MODEL_DTYPE": "bfloat16"}, 1, "DTYPE", "x") == "bfloat16"

    def test_default_when_neither_is_set(self) -> None:
        assert _engine_option({}, 1, "DTYPE", "bfloat16") == "bfloat16"

    def test_whitespace_only_value_is_not_a_value(self) -> None:
        """`MODEL_1_DTYPE=` in a .env file arrives as an empty string, not absent."""
        e = {"MODEL_1_DTYPE": "   ", "MODEL_DTYPE": "float16"}
        assert _engine_option(e, 1, "DTYPE", "x") == "float16"

    def test_n_none_skips_the_numbered_lookup(self) -> None:
        e = {"MODEL_1_DTYPE": "float16"}
        assert _engine_option(e, None, "DTYPE", "bfloat16") == "bfloat16"

    def test_value_is_stripped(self) -> None:
        assert _engine_option({"MODEL_1_DTYPE": " awq \n"}, 1, "DTYPE", "x") == "awq"


class TestEscapeYamlValue:
    @pytest.mark.parametrize("plain", ["mistral-7b", "org/Model-7B-Instruct", "0.9", ""])
    def test_plain_values_pass_through(self, plain: str) -> None:
        assert _escape_yaml_value(plain) == plain

    @pytest.mark.parametrize("hostile", ["a: b", "x{y}", "has#hash", "two\nlines"])
    def test_yaml_special_chars_are_quoted(self, hostile: str) -> None:
        """An unescaped ':' in a model source rewrites the document structure.

        The value becomes a mapping key and the surrounding entry silently
        changes shape, which YAML accepts and Ray Serve then rejects far from
        the cause.
        """
        escaped = _escape_yaml_value(hostile)
        assert escaped != hostile
        assert yaml.safe_load(f"value: {escaped}\n")["value"] == hostile


# ── _validate_schema_values ─────────────────────────────────────────────────


class TestValidateGpuMemoryUtilization:
    @pytest.mark.parametrize("good", ["0.1", "0.9", "1.0", "1"])
    def test_accepts_the_open_unit_interval(self, good: str) -> None:
        _validate_schema_values({"GPU_MEMORY_UTILIZATION": good})

    @pytest.mark.parametrize("bad", ["0", "0.0", "1.1", "-0.5", "2"])
    def test_rejects_out_of_range(self, bad: str, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _validate_schema_values({"GPU_MEMORY_UTILIZATION": bad})
        assert exc.value.code == 1
        assert "GPU_MEMORY_UTILIZATION" in capsys.readouterr().err

    @pytest.mark.parametrize("bad", ["", "abc", "0.9.1", "90%"])
    def test_rejects_non_float(self, bad: str, capsys: pytest.CaptureFixture[str]) -> None:
        """'90%' is the plausible typo: it looks like a utilisation and is not a float."""
        with pytest.raises(SystemExit) as exc:
            _validate_schema_values({"GPU_MEMORY_UTILIZATION": bad})
        assert exc.value.code == 1
        assert "float" in capsys.readouterr().err

    def test_absent_falls_back_to_the_default(self) -> None:
        _validate_schema_values({})


class TestValidateMaxModelLen:
    @pytest.mark.parametrize("good", ["1", "8192", "131072"])
    def test_accepts_positive_integers(self, good: str) -> None:
        _validate_schema_values({"MAX_MODEL_LEN": good})

    @pytest.mark.parametrize("bad", ["0", "-1", "8192.0", "8k", "", " 8192"])
    def test_rejects_anything_else(self, bad: str, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _validate_schema_values({"MAX_MODEL_LEN": bad})
        assert exc.value.code == 1
        assert "MAX_MODEL_LEN" in capsys.readouterr().err


class TestValidateGpuCount:
    @pytest.mark.parametrize("good", ["1", "2", "8"])
    def test_accepts_positive(self, good: str) -> None:
        _validate_schema_values({"GPU_COUNT": good})

    def test_rejects_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _validate_schema_values({"GPU_COUNT": "0"})
        assert exc.value.code == 1
        assert "GPU_COUNT" in capsys.readouterr().err

    @pytest.mark.parametrize("bad", ["", "two", "1.5"])
    def test_rejects_non_integer(self, bad: str, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _validate_schema_values({"GPU_COUNT": bad})
        assert exc.value.code == 1
        assert "inteiro" in capsys.readouterr().err


class TestValidateGpuVram:
    @pytest.mark.parametrize("good", ["24", "40.0", "80"])
    def test_accepts_positive(self, good: str) -> None:
        _validate_schema_values({"GPU_VRAM_GB": good})

    @pytest.mark.parametrize("bad", ["0", "-24"])
    def test_rejects_non_positive(self, bad: str, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _validate_schema_values({"GPU_VRAM_GB": bad})
        assert exc.value.code == 1
        assert "GPU_VRAM_GB" in capsys.readouterr().err

    @pytest.mark.parametrize("bad", ["", "24GB", "twenty"])
    def test_rejects_non_number(self, bad: str, capsys: pytest.CaptureFixture[str]) -> None:
        """'24GB' is the typo the unit in the variable name invites."""
        with pytest.raises(SystemExit) as exc:
            _validate_schema_values({"GPU_VRAM_GB": bad})
        assert exc.value.code == 1
        assert "número" in capsys.readouterr().err


class TestResidentVramBudget:
    """Only models with min_replicas >= 1 compete for VRAM at the same time.

    The check this replaced multiplied utilisation by the *total* model count
    and refused configurations that work: under scale-to-zero a model holds no
    memory until a request wakes it. Five scale-to-zero models on one GPU is
    the deployment this project exists for, and the old formula called it
    impossible.
    """

    def test_many_scale_to_zero_models_fit_on_one_gpu(self) -> None:
        e = {"MODELS_COUNT": "5", "GPU_COUNT": "1", "GPU_MEMORY_UTILIZATION": "0.9"}
        for n in range(1, 6):
            e[f"MODEL_{n}_ID"] = f"m{n}"
        _validate_schema_values(e)

    def test_one_resident_model_fits(self) -> None:
        _validate_schema_values({
            "MODELS_COUNT": "3", "GPU_COUNT": "1", "GPU_MEMORY_UTILIZATION": "0.9",
            "MODEL_1_MIN_REPLICAS": "1",
        })

    def test_two_resident_models_overflow_one_gpu(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _validate_schema_values({
                "MODELS_COUNT": "2", "GPU_COUNT": "1", "GPU_MEMORY_UTILIZATION": "0.9",
                "MODEL_1_ID": "qwen3-8b", "MODEL_1_MIN_REPLICAS": "1",
                "MODEL_2_ID": "mistral-7b", "MODEL_2_MIN_REPLICAS": "1",
            })
        assert exc.value.code == 1
        err = capsys.readouterr().err
        # The message must name the offending models, not just the arithmetic:
        # the operator's next action is to pick one of them to scale to zero.
        assert "qwen3-8b" in err
        assert "mistral-7b" in err
        assert "GPU_COUNT=1" in err

    def test_two_resident_models_fit_on_two_gpus(self) -> None:
        _validate_schema_values({
            "MODELS_COUNT": "2", "GPU_COUNT": "2", "GPU_MEMORY_UTILIZATION": "0.9",
            "MODEL_1_MIN_REPLICAS": "1", "MODEL_2_MIN_REPLICAS": "1",
        })

    def test_unnamed_resident_model_falls_back_to_a_positional_label(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            _validate_schema_values({
                "MODELS_COUNT": "2", "GPU_COUNT": "1", "GPU_MEMORY_UTILIZATION": "0.9",
                "MODEL_1_MIN_REPLICAS": "1", "MODEL_2_MIN_REPLICAS": "1",
            })
        assert "model_1" in capsys.readouterr().err

    def test_empty_min_replicas_counts_as_scale_to_zero(self) -> None:
        """`MODEL_1_MIN_REPLICAS=` in .env must not read as resident."""
        _validate_schema_values({
            "MODELS_COUNT": "2", "GPU_COUNT": "1", "GPU_MEMORY_UTILIZATION": "0.9",
            "MODEL_1_MIN_REPLICAS": "", "MODEL_2_MIN_REPLICAS": "",
        })

    def test_unparseable_models_count_defers_to_collect_env(self) -> None:
        """Returns without raising — _collect_env owns that diagnostic."""
        _validate_schema_values({"MODELS_COUNT": "many"})

    def test_empty_models_count_skips_the_budget_check(self) -> None:
        _validate_schema_values({"MODELS_COUNT": ""})

    def test_zero_models_count_skips_the_budget_check(self) -> None:
        _validate_schema_values({"MODELS_COUNT": "0"})


# ── _collect_env ────────────────────────────────────────────────────────────


class TestCollectEnv:
    def test_single_model_mode_returns_defaults_merged(self, env: dict[str, str]) -> None:
        env.update(SINGLE)
        out = _collect_env()
        assert out["MODEL_ID"] == "mistral-7b"
        assert out["MAX_MODEL_LEN"] == "8192"
        assert out["GPU_COUNT"] == "1"

    def test_missing_required_vars_are_all_named_at_once(
        self, env: dict[str, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Both missing names must appear, not just the first.

        Reporting one at a time turns a single fix into as many container
        restarts as there are unset variables.
        """
        with pytest.raises(SystemExit) as exc:
            _collect_env()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "MODEL_ID" in err
        assert "MODEL_SOURCE" in err

    def test_models_count_is_not_required_in_single_model_mode(
        self, env: dict[str, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            _collect_env()
        assert "MODELS_COUNT" not in capsys.readouterr().err

    def test_multi_model_mode_requires_numbered_vars(
        self, env: dict[str, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.update({"MODELS_COUNT": "2", "GPU_COUNT": "2", "MODEL_1_ID": "a"})
        with pytest.raises(SystemExit) as exc:
            _collect_env()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "MODEL_1_SOURCE" in err
        assert "MODEL_2_ID" in err
        assert "MODEL_2_SOURCE" in err

    def test_multi_model_mode_does_not_require_single_model_vars(
        self, env: dict[str, str]
    ) -> None:
        """Declaring MODELS_COUNT must not also demand the unnumbered pair."""
        env.update({
            "MODELS_COUNT": "1",
            "MODEL_1_ID": "a", "MODEL_1_SOURCE": "org/a",
        })
        out = _collect_env()
        assert "MODEL_ID" not in out

    def test_unparseable_models_count_is_fatal(
        self, env: dict[str, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.update({"MODELS_COUNT": "dois", **SINGLE})
        with pytest.raises(SystemExit) as exc:
            _collect_env()
        assert exc.value.code == 1
        assert "MODELS_COUNT" in capsys.readouterr().err

    def test_empty_models_count_means_single_model_mode(self, env: dict[str, str]) -> None:
        env.update({"MODELS_COUNT": "", **SINGLE})
        assert _collect_env()["MODEL_ID"] == "mistral-7b"

    def test_schema_validation_runs_after_collection(
        self, env: dict[str, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A present-but-invalid value must fail here, not reach the renderer."""
        env.update({**SINGLE, "GPU_MEMORY_UTILIZATION": "1.5"})
        with pytest.raises(SystemExit):
            _collect_env()
        assert "GPU_MEMORY_UTILIZATION" in capsys.readouterr().err


# ── _model_entry / _build_llm_configs ───────────────────────────────────────


class TestModelEntry:
    def test_renders_the_shared_template(self) -> None:
        entry = _model_entry({}, "mid", "org/src", n=1)
        parsed = yaml.safe_load(entry)[0]
        assert parsed["model_loading_config"] == {"model_id": "mid", "model_source": "org/src"}
        assert parsed["engine_kwargs"]["dtype"] == "bfloat16"
        assert parsed["engine_kwargs"]["enable_auto_tool_choice"] is True
        assert "quantization" not in parsed["engine_kwargs"]

    def test_quantization_line_appears_only_when_set(self) -> None:
        entry = _model_entry({"MODEL_1_QUANTIZATION": "awq"}, "mid", "org/src", n=1)
        assert yaml.safe_load(entry)[0]["engine_kwargs"]["quantization"] == "awq"


class TestBuildLlmConfigs:
    def test_single_model_produces_one_entry(self) -> None:
        entries = yaml.safe_load(_build_llm_configs(dict(SINGLE)))
        assert len(entries) == 1

    def test_single_model_applies_numbered_overrides(self) -> None:
        """MODEL_1_* in single-model mode is the shape .env.example suggests."""
        e = {**SINGLE, "MODEL_1_QUANTIZATION": "awq"}
        assert yaml.safe_load(_build_llm_configs(e))[0]["engine_kwargs"]["quantization"] == "awq"

    @pytest.mark.parametrize(
        "e",
        [
            {"MODEL_ID": "a"},
            {"MODEL_SOURCE": "org/a"},
            {"MODEL_ID": "  ", "MODEL_SOURCE": "org/a"},
            {"MODEL_ID": "a", "MODEL_SOURCE": "   "},
        ],
    )
    def test_incomplete_single_model_is_fatal(
        self, e: dict[str, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _build_llm_configs(e)
        assert exc.value.code == 1
        assert "single-model" in capsys.readouterr().err

    def test_multi_model_produces_one_entry_each(self) -> None:
        e = {
            "MODELS_COUNT": "3", "GPU_COUNT": "1",
            "MODEL_1_ID": "a", "MODEL_1_SOURCE": "org/a",
            "MODEL_2_ID": "b", "MODEL_2_SOURCE": "org/b",
            "MODEL_3_ID": "c", "MODEL_3_SOURCE": "org/c",
        }
        entries = yaml.safe_load(_build_llm_configs(e))
        assert [x["model_loading_config"]["model_id"] for x in entries] == ["a", "b", "c"]

    def test_declaring_more_models_than_defined_is_fatal(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The silent drop this replaced rendered one model and reported success."""
        e = {"MODELS_COUNT": "2", "MODEL_1_ID": "a", "MODEL_1_SOURCE": "org/a"}
        with pytest.raises(SystemExit) as exc:
            _build_llm_configs(e)
        assert exc.value.code == 1
        assert "MODEL_2_ID" in capsys.readouterr().err

    def test_unparseable_models_count_degrades_to_single_model(self) -> None:
        """_collect_env already refused this; the renderer must not crash on it."""
        entries = yaml.safe_load(_build_llm_configs({**SINGLE, "MODELS_COUNT": "x"}))
        assert len(entries) == 1

    def test_negative_models_count_degrades_to_single_model(self) -> None:
        entries = yaml.safe_load(_build_llm_configs({**SINGLE, "MODELS_COUNT": "-1"}))
        assert len(entries) == 1


# ── _substitute ─────────────────────────────────────────────────────────────


class TestSubstitute:
    def test_replaces_placeholders(self) -> None:
        out = _substitute("host: ${HOST}\n", {"HOST": "0.0.0.0"})
        assert out == "host: 0.0.0.0\n"

    def test_escapes_hostile_values(self) -> None:
        out = _substitute("value: ${V}\n", {"V": "a: b"})
        assert yaml.safe_load(out)["value"] == "a: b"

    def test_unresolved_placeholder_is_fatal_and_names_it(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A leftover ${VAR} is valid YAML — it becomes the literal string.

        Only model_id and model_source are checked downstream, so a typo in
        any other field used to reach the engine as text and fail somewhere
        far from its cause.
        """
        with pytest.raises(SystemExit) as exc:
            _substitute("a: ${NOPE}\nb: ${ALSO_NOPE}\n", {})
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "NOPE" in err
        assert "ALSO_NOPE" in err

    def test_each_unresolved_name_is_reported_once(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            _substitute("a: ${X}\nb: ${X}\n", {})
        assert capsys.readouterr().err.count("X") == 1

    def test_marker_on_the_llm_configs_key_is_expanded(self) -> None:
        out = _substitute(TEMPLATE, dict(SINGLE))
        assert len(yaml.safe_load(out)["applications"][0]["args"]["llm_configs"]) == 1

    def test_marker_named_in_a_comment_is_left_alone(self) -> None:
        """A header comment naming the marker must not splice YAML into the file.

        This bit during development: the comment matched, substitution fired
        there, and the rendered file grew a second YAML document.
        """
        template = "# cites ##LLM_CONFIGS## by name\n" + TEMPLATE
        parsed = yaml.safe_load(_substitute(template, dict(SINGLE)))
        assert len(parsed["applications"][0]["args"]["llm_configs"]) == 1

    def test_template_without_the_marker_is_substituted_verbatim(self) -> None:
        out = _substitute("model: ${MODEL_ID}\n", dict(SINGLE))
        assert yaml.safe_load(out)["model"] == "mistral-7b"

    def test_static_example_entries_below_the_marker_are_dropped(self) -> None:
        """The template may carry a readable example; it must never be rendered."""
        template = TEMPLATE + """        - model_loading_config:
            model_id: stale-example
            model_source: org/stale
"""
        parsed = yaml.safe_load(_substitute(template, dict(SINGLE)))
        ids = [c["model_loading_config"]["model_id"]
               for c in parsed["applications"][0]["args"]["llm_configs"]]
        assert ids == ["mistral-7b"]

    def test_a_top_level_key_after_the_marker_survives(self) -> None:
        """Dropping the example must stop at the next top-level section.

        Swallowing everything below the marker would silently delete real
        configuration that happens to be declared after llm_configs.
        """
        template = TEMPLATE + """        - model_loading_config:
            model_id: stale
            model_source: org/stale
grpc_options:
  port: 9000
"""
        parsed = yaml.safe_load(_substitute(template, dict(SINGLE)))
        assert parsed["grpc_options"] == {"port": 9000}
        ids = [c["model_loading_config"]["model_id"]
               for c in parsed["applications"][0]["args"]["llm_configs"]]
        assert ids == ["mistral-7b"]

    def test_blank_lines_below_the_marker_do_not_end_the_drop(self) -> None:
        template = TEMPLATE + """        - model_loading_config:
            model_id: stale
            model_source: org/stale

        - model_loading_config:
            model_id: stale2
            model_source: org/stale2
"""
        parsed = yaml.safe_load(_substitute(template, dict(SINGLE)))
        ids = [c["model_loading_config"]["model_id"]
               for c in parsed["applications"][0]["args"]["llm_configs"]]
        assert ids == ["mistral-7b"]


# ── _validate_yaml ──────────────────────────────────────────────────────────


class TestValidateYaml:
    def test_accepts_a_well_formed_render(self) -> None:
        _validate_yaml(_substitute(TEMPLATE, dict(SINGLE)))

    def test_malformed_yaml_is_fatal(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _validate_yaml("a: [unclosed\n")
        assert exc.value.code == 1
        assert "invalid YAML" in capsys.readouterr().err

    @pytest.mark.parametrize("empty", ["", "\n", "# only a comment\n"])
    def test_empty_render_is_fatal(
        self, empty: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _validate_yaml(empty)
        assert exc.value.code == 1
        assert "empty" in capsys.readouterr().err

    def test_missing_applications_is_fatal(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _validate_yaml("proxy_location: EveryNode\n")
        assert exc.value.code == 1
        assert "applications" in capsys.readouterr().err

    def test_empty_applications_is_fatal(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _validate_yaml("applications: []\n")
        assert exc.value.code == 1
        assert "applications" in capsys.readouterr().err

    def test_missing_llm_configs_is_fatal(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _validate_yaml("applications:\n  - name: llms\n    args: {}\n")
        assert exc.value.code == 1
        assert "llm_configs" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "entry",
        [
            "{model_loading_config: {model_source: org/a}}",
            "{model_loading_config: {model_id: a}}",
            "{model_loading_config: {}}",
            "{engine_kwargs: {dtype: bfloat16}}",
        ],
    )
    def test_entry_without_both_identifiers_is_fatal(
        self, entry: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An entry missing model_source loads no weights and reports no error."""
        rendered = f"applications:\n  - args:\n      llm_configs:\n        - {entry}\n"
        with pytest.raises(SystemExit) as exc:
            _validate_yaml(rendered)
        assert exc.value.code == 1
        assert "missing model_id or model_source" in capsys.readouterr().err

    def test_the_failing_index_is_named(self, capsys: pytest.CaptureFixture[str]) -> None:
        """With eight models, 'an entry is broken' is not a diagnostic."""
        rendered = (
            "applications:\n  - args:\n      llm_configs:\n"
            "        - {model_loading_config: {model_id: a, model_source: org/a}}\n"
            "        - {model_loading_config: {model_id: b}}\n"
        )
        with pytest.raises(SystemExit):
            _validate_yaml(rendered)
        assert "llm_config[1]" in capsys.readouterr().err


# ── _log_diagnostics ────────────────────────────────────────────────────────


class TestLogDiagnostics:
    def test_single_model_summary_goes_to_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """stdout carries the rendered YAML under --dry-run; a summary there corrupts it."""
        _log_diagnostics({**SINGLE, "MAX_MODEL_LEN": "8192"})
        out, err = capsys.readouterr()
        assert out == ""
        assert "mistral-7b" in err
        assert "max_len=8192" in err

    def test_multi_model_summary_lists_every_model(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _log_diagnostics({
            "MODELS_COUNT": "2", "MODEL_1_ID": "a", "MODEL_2_ID": "b",
        })
        err = capsys.readouterr().err
        assert "2 model(s)" in err
        assert "a, b" in err

    def test_multi_model_summary_skips_unnamed_entries(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _log_diagnostics({"MODELS_COUNT": "2", "MODEL_1_ID": "a"})
        assert "a" in capsys.readouterr().err

    def test_unparseable_models_count_falls_back_to_single(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _log_diagnostics({"MODELS_COUNT": "x", **SINGLE})
        assert "model=mistral-7b" in capsys.readouterr().err

    def test_empty_models_count_falls_back_to_single(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _log_diagnostics({"MODELS_COUNT": "", **SINGLE})
        assert "model=mistral-7b" in capsys.readouterr().err

    def test_absent_values_render_as_a_question_mark(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _log_diagnostics({})
        err = capsys.readouterr().err
        assert "model=?" in err
        assert "source=?" in err


# ── _render_litellm_config ──────────────────────────────────────────────────


class TestRenderLitellmConfig:
    def test_single_model_gets_one_entry_pointing_at_ray(self) -> None:
        cfg = yaml.safe_load(_render_litellm_config(dict(SINGLE)))
        assert len(cfg["model_list"]) == 1
        params = cfg["model_list"][0]["litellm_params"]
        assert cfg["model_list"][0]["model_name"] == "mistral-7b"
        assert params["model"] == "openai/mistral-7b"
        assert params["api_base"] == "http://ray-head:8000/v1"

    def test_master_key_stays_an_env_reference(self) -> None:
        """This file is written to disk; the real key must never reach it.

        LiteLLM resolves ``os.environ/NAME`` at container startup, so the
        rendered artefact is safe to leave in the repo root.
        """
        raw = _render_litellm_config(dict(SINGLE))
        assert "os.environ/LITELLM_MASTER_KEY" in raw
        cfg = yaml.safe_load(raw)
        assert cfg["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"

    def test_metrics_endpoint_auth_is_opted_out(self) -> None:
        """LiteLLM 1.84.0+ requires auth on /metrics; Prometheus sends no token."""
        cfg = yaml.safe_load(_render_litellm_config(dict(SINGLE)))
        assert cfg["litellm_settings"]["require_auth_for_metrics_endpoint"] is False

    def test_the_three_tiers_are_present_with_distinct_limits(self) -> None:
        cfg = yaml.safe_load(_render_litellm_config(dict(SINGLE)))
        teams = cfg["litellm_settings"]["default_team_settings"]
        by_id = {t["team_id"]: t for t in teams}
        assert set(by_id) == {"hard", "regular", "light"}
        assert by_id["hard"]["rpm_limit"] > by_id["regular"]["rpm_limit"]
        assert by_id["regular"]["rpm_limit"] > by_id["light"]["rpm_limit"]
        assert by_id["hard"]["tpm_limit"] > by_id["regular"]["tpm_limit"]
        assert by_id["regular"]["tpm_limit"] > by_id["light"]["tpm_limit"]

    def test_multi_model_gets_one_entry_each(self) -> None:
        cfg = yaml.safe_load(_render_litellm_config({
            "MODELS_COUNT": "2",
            "MODEL_1_ID": "a", "MODEL_1_SOURCE": "org/a",
            "MODEL_2_ID": "b", "MODEL_2_SOURCE": "org/b",
        }))
        assert [m["model_name"] for m in cfg["model_list"]] == ["a", "b"]

    def test_a_blank_numbered_id_is_skipped_not_rendered_empty(self) -> None:
        """An empty model_name reaches LiteLLM as a route that matches nothing."""
        cfg = yaml.safe_load(_render_litellm_config({
            "MODELS_COUNT": "2", "MODEL_1_ID": "a", "MODEL_2_ID": "   ",
        }))
        assert [m["model_name"] for m in cfg["model_list"]] == ["a"]

    def test_unparseable_models_count_degrades_to_single(self) -> None:
        cfg = yaml.safe_load(_render_litellm_config({**SINGLE, "MODELS_COUNT": "x"}))
        assert [m["model_name"] for m in cfg["model_list"]] == ["mistral-7b"]

    def test_no_model_at_all_yields_an_empty_list_not_a_crash(self) -> None:
        cfg = yaml.safe_load(_render_litellm_config({}))
        assert cfg["model_list"] == []

    def test_no_shell_style_placeholder_survives(self) -> None:
        """LiteLLM does not expand ${VAR}; it serves the literal text.

        Every request against such a config fails with "model not found",
        and the config itself looks correct to a reader.
        """
        raw = _render_litellm_config(dict(SINGLE))
        assert "${" not in raw


# ── _write_rendered_files ───────────────────────────────────────────────────


class TestWriteRenderedFiles:
    def test_writes_both_files_and_returns_their_paths(self, tmp_path: Path) -> None:
        serve, litellm = _write_rendered_files("serve: yes\n", "litellm: yes\n", tmp_path)
        assert serve == tmp_path / "rendered_serve_config.yaml"
        assert litellm == tmp_path / LITELLM_RENDERED_FILENAME
        assert serve.read_text(encoding="utf-8") == "serve: yes\n"
        assert litellm.read_text(encoding="utf-8") == "litellm: yes\n"

    def test_overwrites_a_stale_render(self, tmp_path: Path) -> None:
        (tmp_path / "rendered_serve_config.yaml").write_text("old\n", encoding="utf-8")
        serve, _ = _write_rendered_files("new\n", "x\n", tmp_path)
        assert serve.read_text(encoding="utf-8") == "new\n"

    def test_utf8_survives_the_round_trip(self, tmp_path: Path) -> None:
        serve, _ = _write_rendered_files("nome: açaí\n", "x\n", tmp_path)
        assert "açaí" in serve.read_text(encoding="utf-8")

    def test_unwritable_target_is_fatal_and_names_the_directory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A read-only repo root must fail loudly, not leave a half-written pair."""
        missing = tmp_path / "does" / "not" / "exist"
        with pytest.raises(SystemExit) as exc:
            _write_rendered_files("a\n", "b\n", missing)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert str(missing) in err
        assert "writable" in err


# ── Public API: render / render_litellm_config / render_file ────────────────


class TestRender:
    def test_renders_from_a_template_string(self, env: dict[str, str]) -> None:
        parsed = yaml.safe_load(render(TEMPLATE, overrides=SINGLE))
        assert parsed["applications"][0]["args"]["llm_configs"][0][
            "model_loading_config"]["model_id"] == "mistral-7b"

    def test_reads_the_ambient_environment_when_no_overrides(
        self, env: dict[str, str]
    ) -> None:
        env.update(SINGLE)
        assert "mistral-7b" in render(TEMPLATE)

    def test_overrides_win_over_the_environment(self, env: dict[str, str]) -> None:
        env.update(SINGLE)
        out = render(TEMPLATE, overrides={"MODEL_ID": "override-me"})
        assert "override-me" in out
        assert "mistral-7b" not in out

    def test_invalid_override_is_fatal(
        self, env: dict[str, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            render(TEMPLATE, overrides={**SINGLE, "MAX_MODEL_LEN": "0"})
        assert "MAX_MODEL_LEN" in capsys.readouterr().err


class TestRenderLitellmPublic:
    def test_renders_from_overrides(self, env: dict[str, str]) -> None:
        cfg = yaml.safe_load(render_litellm_config(overrides=SINGLE))
        assert cfg["model_list"][0]["model_name"] == "mistral-7b"

    def test_reads_the_ambient_environment(self, env: dict[str, str]) -> None:
        env.update(SINGLE)
        assert yaml.safe_load(render_litellm_config())["model_list"][0][
            "model_name"] == "mistral-7b"

    def test_invalid_override_is_fatal(
        self, env: dict[str, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            render_litellm_config(overrides={**SINGLE, "GPU_COUNT": "0"})
        assert "GPU_COUNT" in capsys.readouterr().err


class TestRenderFile:
    def test_reads_renders_and_reports_the_env_used(
        self, env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.update(SINGLE)
        p = tmp_path / TEMPLATE_FILENAME
        p.write_text(TEMPLATE, encoding="utf-8")
        rendered, used = render_file(p)
        assert "mistral-7b" in rendered
        assert used["MODEL_ID"] == "mistral-7b"
        assert used["MAX_MODEL_LEN"] == "8192"
        assert "mistral-7b" in capsys.readouterr().err

    def test_accepts_a_string_path(self, env: dict[str, str], tmp_path: Path) -> None:
        env.update(SINGLE)
        p = tmp_path / TEMPLATE_FILENAME
        p.write_text(TEMPLATE, encoding="utf-8")
        rendered, _ = render_file(str(p))
        assert "mistral-7b" in rendered

    def test_missing_template_is_fatal(self, env: dict[str, str], tmp_path: Path) -> None:
        env.update(SINGLE)
        with pytest.raises(SystemExit):
            render_file(tmp_path / "absent.yaml")


# ── main() ──────────────────────────────────────────────────────────────────


@pytest.fixture
def repo_template(repo_root: Path) -> Path:
    """The real template main() resolves relative to scripts/."""
    return repo_root / TEMPLATE_FILENAME


class TestMainDryRun:
    def test_prints_the_render_to_stdout_and_writes_nothing(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        env.update(SINGLE)
        monkeypatch.setattr(sys, "argv", ["render_config.py", "--dry-run"])
        sentinel = tmp_path / "must-not-appear.yaml"
        monkeypatch.setattr("scripts.render_config.RENDERED_PATH", sentinel)
        main()
        out = capsys.readouterr().out
        assert yaml.safe_load(out)["applications"][0]["args"]["llm_configs"]
        assert not sentinel.exists()

    def test_prints_no_diagnostics_that_would_corrupt_stdout(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """--dry-run output is piped into a YAML parser by the deploy path."""
        env.update(SINGLE)
        monkeypatch.setattr(sys, "argv", ["render_config.py", "--dry-run"])
        main()
        assert yaml.safe_load(capsys.readouterr().out) is not None

    def test_missing_env_is_fatal(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["render_config.py", "--dry-run"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        assert "MODEL_ID" in capsys.readouterr().err


class TestMainRenderAll:
    def test_writes_both_configs_to_the_repo_root(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        repo_template: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``./idia deploy local`` depends on both files landing before compose up."""
        env.update(SINGLE)
        fake_scripts = tmp_path / "scripts"
        fake_scripts.mkdir()
        (tmp_path / TEMPLATE_FILENAME).write_text(
            repo_template.read_text(encoding="utf-8"), encoding="utf-8"
        )
        monkeypatch.setattr(sys, "argv", ["render_config.py", "--render-all"])
        monkeypatch.setattr(
            "scripts.render_config.Path",
            _PathStubbingFile(fake_scripts / "render_config.py"),
        )
        main()
        serve = tmp_path / "rendered_serve_config.yaml"
        litellm = tmp_path / LITELLM_RENDERED_FILENAME
        assert yaml.safe_load(serve.read_text(encoding="utf-8"))["applications"]
        assert yaml.safe_load(litellm.read_text(encoding="utf-8"))["model_list"]
        err = capsys.readouterr().err
        assert str(serve) in err
        assert str(litellm) in err

    def test_render_all_takes_precedence_over_dry_run(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        repo_template: Path,
    ) -> None:
        """Both flags at once must write files, not print and exit."""
        env.update(SINGLE)
        fake_scripts = tmp_path / "scripts"
        fake_scripts.mkdir()
        (tmp_path / TEMPLATE_FILENAME).write_text(
            repo_template.read_text(encoding="utf-8"), encoding="utf-8"
        )
        monkeypatch.setattr(sys, "argv", ["render_config.py", "--dry-run", "--render-all"])
        monkeypatch.setattr(
            "scripts.render_config.Path",
            _PathStubbingFile(fake_scripts / "render_config.py"),
        )
        main()
        assert (tmp_path / LITELLM_RENDERED_FILENAME).is_file()


class TestMainNormalMode:
    def test_writes_the_render_then_execs_serve(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        env.update(SINGLE)
        monkeypatch.setattr(sys, "argv", ["render_config.py"])
        target = tmp_path / "idia_serve_config.yaml"
        monkeypatch.setattr("scripts.render_config.RENDERED_PATH", target)

        seen: list[tuple[str, ...]] = []

        def _fake_execlp(*args: str) -> None:
            seen.append(args)
            raise _ExecCalled

        monkeypatch.setattr(os, "execlp", _fake_execlp)
        with pytest.raises(_ExecCalled):
            main()

        assert yaml.safe_load(target.read_text(encoding="utf-8"))["applications"]
        assert seen == [("serve", "serve", "run", str(target))]
        assert str(target) in capsys.readouterr().err

    def test_execlp_failure_exits_nonzero(
        self, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """execlp returning at all means serve is not on PATH.

        Falling through silently would leave the container exiting 0, which
        Compose reads as a clean shutdown and never restarts.
        """
        env.update(SINGLE)
        monkeypatch.setattr(sys, "argv", ["render_config.py"])
        monkeypatch.setattr(
            "scripts.render_config.RENDERED_PATH", tmp_path / "out.yaml"
        )
        monkeypatch.setattr(os, "execlp", lambda *a: None)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        assert "execlp failed" in capsys.readouterr().err


class _PathStubbingFile:
    """Stand-in for ``Path`` that redirects only ``Path(__file__)``.

    ``main()`` derives the repo root from ``Path(__file__).resolve().parent``,
    so pointing that one call at a temporary tree is the only way to test
    ``--render-all`` without writing into the working copy. Every other
    ``Path(...)`` call is forwarded untouched.
    """

    def __init__(self, fake_file: Path) -> None:
        self._fake_file = fake_file

    def __call__(self, *args: object, **kwargs: object) -> Path:
        if len(args) == 1 and isinstance(args[0], str) and args[0].endswith(
            "render_config.py"
        ):
            return self._fake_file
        return Path(*args, **kwargs)  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        return getattr(Path, name)
