"""Tests for scripts/colleague.sh — unified user provisioning.

Everything here runs without Docker, without a LiteLLM server and without
touching the operator's real .env: the script accepts IDIA_ENV_FILE so the
tests point it at a throwaway file, and only exercise the paths that stop
before any network or container call (--help, tiers, --dry-run, validation).

Three of these are regression guards for defects found in the audit that
produced this script's rewrite:

  * no literal secrets or IP addresses in the source (issue #2)
  * values reach Python through argv, never through source interpolation,
    so a name with an apostrophe cannot break or inject (issue #4)
  * no associative arrays, so it runs on the bash 3.2 that ships with
    macOS (issue #9)
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ENV_TEMPLATE = """\
LITELLM_MASTER_KEY=sk-test-master-key
MODEL_ID=mistral-7b
MODEL_SOURCE=mistralai/Mistral-7B-Instruct-v0.3
IDIA_PUBLIC_HOST=idia.example.org
"""

# A name that terminates the string literal it would be pasted into, plus a
# payload that would execute if the value were interpolated into Python source.
HOSTILE_NAME = "Ana D'Ávila'); import os; os.system('touch /tmp/idia_pwned'); #"


@pytest.fixture
def colleague(repo_root: Path) -> Path:
    path = repo_root / "scripts" / "colleague.sh"
    if not path.is_file():
        pytest.skip("scripts/colleague.sh not present")
    return path


@pytest.fixture
def fake_env(tmp_path: Path) -> Path:
    env = tmp_path / "test.env"
    env.write_text(ENV_TEMPLATE, encoding="utf-8")
    return env


def _code_lines(path: Path) -> list[str]:
    """Source lines with whole-line comments dropped.

    Lets a guard assert on what the script *does* without tripping over a
    comment that names the very construct being forbidden.
    """
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]


def run(script: Path, *args: str, env_file: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if env_file is not None:
        env["IDIA_ENV_FILE"] = str(env_file)
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


# ── Basics ──────────────────────────────────────────────────────────────


class TestColleagueBasics:
    """The script exists, is executable, and describes itself."""

    def test_is_executable(self, colleague: Path) -> None:
        assert os.access(colleague, os.X_OK), "colleague.sh must be executable"

    def test_help_exits_zero(self, colleague: Path) -> None:
        result = run(colleague, "--help")
        assert result.returncode == 0, result.stderr

    def test_help_lists_every_subcommand(self, colleague: Path) -> None:
        out = run(colleague, "--help").stdout
        for sub in ("create", "key", "status", "revoke", "tiers"):
            assert sub in out, f"--help does not mention '{sub}'"

    def test_unknown_command_fails(self, colleague: Path) -> None:
        result = run(colleague, "naoexiste")
        assert result.returncode != 0

    def test_missing_env_file_fails_with_guidance(self, colleague: Path, tmp_path: Path) -> None:
        result = run(colleague, "tiers", env_file=tmp_path / "absent.env")
        assert result.returncode != 0
        assert ".env" in result.stderr


# ── Tiers ───────────────────────────────────────────────────────────────


class TestTiers:
    """Tier definitions resolve, and granted models come from the .env."""

    def test_tiers_exits_zero(self, colleague: Path, fake_env: Path) -> None:
        result = run(colleague, "tiers", env_file=fake_env)
        assert result.returncode == 0, result.stderr

    def test_all_four_tiers_present(self, colleague: Path, fake_env: Path) -> None:
        out = run(colleague, "tiers", env_file=fake_env).stdout
        for tier in ("light", "regular", "heavy", "classroom"):
            assert tier in out, f"tier '{tier}' missing from output"

    def test_models_come_from_env_not_hardcoded(self, colleague: Path, fake_env: Path) -> None:
        """The tier listing must echo the model configured in .env.

        Guards the defect where every tier advertised a hardcoded model name
        that no longer matched the deployed server.
        """
        out = run(colleague, "tiers", env_file=fake_env).stdout
        assert "mistral-7b" in out

    def test_invalid_tier_rejected(self, colleague: Path, fake_env: Path) -> None:
        result = run(
            colleague, "create", "x@y.z", "Nome", "--tier", "inexistente", "--dry-run",
            env_file=fake_env,
        )
        assert result.returncode != 0
        assert "inexistente" in result.stderr


# ── Dry run ─────────────────────────────────────────────────────────────


class TestDryRun:
    """--dry-run reports the plan and creates nothing."""

    def test_dry_run_exits_zero(self, colleague: Path, fake_env: Path) -> None:
        result = run(colleague, "create", "ana@idia.org", "Ana Costa", "--dry-run",
                     env_file=fake_env)
        assert result.returncode == 0, result.stderr

    def test_dry_run_applies_tier_defaults(self, colleague: Path, fake_env: Path) -> None:
        out = run(colleague, "create", "ana@idia.org", "Ana", "--tier", "classroom",
                  "--dry-run", env_file=fake_env).stdout
        assert "20" in out, "classroom budget not shown"
        assert "300" in out, "classroom RPM not shown"

    def test_dry_run_announces_configured_host(self, colleague: Path, fake_env: Path) -> None:
        """Credentials must reference IDIA_PUBLIC_HOST, never a baked-in address."""
        result = run(colleague, "create", "ana@idia.org", "Ana", "--no-openwebui",
                     "--dry-run", env_file=fake_env)
        assert result.returncode == 0


# ── Regression guards ───────────────────────────────────────────────────


class TestNoInjection:
    """Values must reach Python via argv, never via source interpolation."""

    def test_apostrophe_name_survives(self, colleague: Path, fake_env: Path) -> None:
        """A name with an apostrophe must not abort the run.

        Before the rewrite this produced a Python SyntaxError halfway through
        provisioning, leaving an orphan LiteLLM key behind.
        """
        result = run(colleague, "create", "ana@idia.org", "Ana D'Ávila", "--dry-run",
                     env_file=fake_env)
        assert result.returncode == 0, result.stderr
        assert "Ana D'Ávila" in result.stdout, "name was mangled"

    def test_hostile_payload_is_inert(self, colleague: Path, fake_env: Path) -> None:
        marker = Path("/tmp/idia_pwned")
        if marker.exists():
            marker.unlink()
        result = run(colleague, "create", "ana@idia.org", HOSTILE_NAME, "--dry-run",
                     env_file=fake_env)
        assert result.returncode == 0, result.stderr
        assert not marker.exists(), "interpolated payload executed"

    def test_python_blocks_use_quoted_heredocs(self, colleague: Path) -> None:
        """Every embedded Python block must use <<'PY' — the quoted form.

        An unquoted heredoc would let bash expand ${...} inside the Python
        source, which is exactly the defect this guards against.
        """
        source = colleague.read_text(encoding="utf-8")
        assert "<<'PY'" in source
        assert '<<"PY"' not in source, "unquoted heredoc allows bash expansion"
        assert "<<PY" not in source, "unquoted heredoc allows bash expansion"


@pytest.mark.security
class TestNoEmbeddedSecrets:
    """No credential or address may be baked into the source."""

    def test_no_literal_api_key(self, colleague: Path) -> None:
        source = colleague.read_text(encoding="utf-8")
        leaked = re.findall(r"\bsk-[A-Za-z0-9_-]{8,}", source)
        assert not leaked, f"literal API key(s) in source: {leaked}"

    def test_no_literal_ip_address(self, colleague: Path) -> None:
        source = colleague.read_text(encoding="utf-8")
        # Ignore version-like strings by requiring a full dotted quad.
        found = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", source)
        assert not found, f"literal IP address(es) in source: {found}"

    def test_public_host_comes_from_env(self, colleague: Path) -> None:
        source = colleague.read_text(encoding="utf-8")
        assert "IDIA_PUBLIC_HOST" in source

    def test_env_example_documents_provisioning_vars(self, repo_root: Path) -> None:
        content = (repo_root / ".env.example").read_text(encoding="utf-8")
        for var in ("IDIA_PUBLIC_HOST", "OWUI_DISCOVERY_KEY", "OWUI_CONTAINER"):
            assert var in content, f".env.example does not document {var}"


class TestBash32Compatible:
    """The script must run on the bash 3.2 that ships with macOS."""

    def test_no_associative_arrays(self, colleague: Path) -> None:
        code = _code_lines(colleague)
        offenders = [ln for ln in code if "declare -A" in ln]
        assert not offenders, f"declare -A requires bash 4+: {offenders}"

    def test_shebang_is_env_bash(self, colleague: Path) -> None:
        first = colleague.read_text(encoding="utf-8").splitlines()[0]
        assert first == "#!/usr/bin/env bash"


# ── CLI wiring ──────────────────────────────────────────────────────────


class TestIdiaColleagueRouting:
    """./idia colleague must reach the script."""

    def test_colleague_in_idia_help(self, repo_root: Path) -> None:
        result = subprocess.run(
            ["bash", str(repo_root / "idia"), "--help"],
            capture_output=True, text=True, timeout=30,
        )
        assert "colleague" in result.stdout

    def test_idia_colleague_help_exits_zero(self, repo_root: Path) -> None:
        """Guards the unbound-variable defect that made this route abort."""
        result = subprocess.run(
            ["bash", str(repo_root / "idia"), "colleague", "--help"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "unbound variable" not in result.stderr
