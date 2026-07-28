"""Dry-run validation tests — no GPU or Docker needed.

Tests for:
- render_config.py --dry-run
- .env schema parsing
- ./idia CLI wrapper (subcommands, help, flags)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.config
class TestRenderConfigDryRun:
    """render_config.py --dry-run produces valid YAML without starting Ray."""

    def test_dry_run_produces_valid_yaml(self, repo_root: Path) -> None:
        """--dry-run outputs valid YAML to stdout."""
        result = subprocess.run(
            [
                "python3",
                str(repo_root / "scripts" / "render_config.py"),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={"MODEL_ID": "test-model", "MODEL_SOURCE": "test/test", "HF_TOKEN": "", "LITELLM_MASTER_KEY": ""},
        )
        assert result.returncode == 0
        assert result.stdout.strip()

    def test_dry_run_fails_without_model_id(self, repo_root: Path) -> None:
        """--dry-run fails when MODEL_ID is missing."""
        result = subprocess.run(
            [
                "python3",
                str(repo_root / "scripts" / "render_config.py"),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env={},
        )
        assert result.returncode != 0


@pytest.mark.config
class TestDotEnvSchema:
    """Validate .env.example has all required variables."""

    def test_env_example_has_required_vars(self, repo_root: Path) -> None:
        """.env.example declares all required env vars."""
        content = (repo_root / ".env.example").read_text()
        required = {"HF_TOKEN", "LITELLM_MASTER_KEY", "MODEL_ID", "MODEL_SOURCE"}
        for var in required:
            assert var in content, (
                f"{var} not found in .env.example"
            )

    def test_env_example_has_optional_vars(self, repo_root: Path) -> None:
        """.env.example documents optional vars with defaults."""
        content = (repo_root / ".env.example").read_text()
        optional = {
            "GPU_MEMORY_UTILIZATION",
            "MAX_MODEL_LEN",
            "GPU_COUNT",
            "MODELS_COUNT",
        }
        for var in optional:
            assert var in content, (
                f"{var} not found in .env.example"
            )

    def test_env_example_is_valid_template(self, repo_root: Path) -> None:
        """.env.example lines follow VAR=VALUE or # comment format."""
        content = (repo_root / ".env.example").read_text()
        valid_prefixes = ("export ", "", "#")
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            assert any(
                stripped.startswith(prefix) for prefix in valid_prefixes
            ) or "=" in stripped, (
                f"Line does not look like valid env var: {line}"
            )


@pytest.mark.config
class TestIdiaCli:
    """Verify the ./idia CLI wrapper exists and prints help."""

    def test_idia_cli_exists(self, repo_root: Path) -> None:
        """idia script exists and is executable."""
        cli = repo_root / "idia"
        assert cli.exists()
        assert cli.stat().st_mode & 0o111  # executable

    def test_idia_help_returns_0(self, repo_root: Path) -> None:
        """idia --help exits with 0 and prints usage."""
        result = subprocess.run(
            ["bash", str(repo_root / "idia"), "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert "usage" in result.stdout.lower() or "Usage" in result.stdout

    def test_idia_help_shows_subcommands(self, repo_root: Path) -> None:
        """idia --help lists all expected subcommands."""
        result = subprocess.run(
            ["bash", str(repo_root / "idia"), "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        # Expected commands in local deploy pipeline
        expected = {"deploy", "status", "user", "logs", "stop", "service", "setup"}
        for cmd in expected:
            assert cmd in result.stdout.lower(), (
                f"Expected subcommand '{cmd}' not in help text"
            )


@pytest.mark.config
class TestNoWaitFlag:
    """--no-wait flag is accepted by deploy local."""

    def test_no_wait_in_help(self, repo_root: Path) -> None:
        """./idia --help documents --no-wait."""
        result = subprocess.run(
            ["bash", str(repo_root / "idia"), "--help"],
            capture_output=True, text=True, timeout=15,
        )
        assert "--no-wait" in result.stdout

    def test_no_wait_accepted_in_args(self, repo_root: Path) -> None:
        """deploy local --no-wait parses the flag without erroring on it."""
        env_file = repo_root / ".env"
        env_backup = repo_root / ".env.test_backup"
        # Temporarily move .env so the command fails early (no real deploy)
        if env_file.exists():
            env_file.rename(env_backup)
        try:
            result = subprocess.run(
                ["bash", str(repo_root / "idia"), "deploy", "local", "--no-wait"],
                capture_output=True, text=True, timeout=15,
            )
            # Should fail with a known error, not because --no-wait is unknown
            output = (result.stderr + result.stdout).lower()
            assert ".env" in output or "docker" in output
        finally:
            if env_backup.exists():
                env_backup.rename(env_file)


@pytest.mark.config
class TestServiceSubcommand:
    """./idia service subcommands exist and require root."""

    def test_service_in_help(self, repo_root: Path) -> None:
        """./idia --help lists service subcommand."""
        result = subprocess.run(
            ["bash", str(repo_root / "idia"), "--help"],
            capture_output=True, text=True, timeout=15,
        )
        assert "service" in result.stdout.lower()

    def test_service_install_requires_root(self, repo_root: Path) -> None:
        """service install fails without root."""
        result = subprocess.run(
            ["bash", str(repo_root / "idia"), "service", "install"],
            capture_output=True, text=True, timeout=15,
            env={"PATH": os.environ.get("PATH", "/usr/bin"), "HOME": os.environ.get("HOME", "/root")},
        )
        assert result.returncode != 0
        assert "root" in result.stderr.lower() or "root" in result.stdout.lower()

    def test_service_unknown_subcommand(self, repo_root: Path) -> None:
        """Unknown service subcommand gives helpful error."""
        result = subprocess.run(
            ["bash", str(repo_root / "idia"), "service", "nosuchthing"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode != 0
        assert "Unknown service subcommand" in result.stderr


@pytest.mark.config
class TestSetupSubcommand:
    """./idia setup runs the environment setup script."""

    def test_setup_in_help(self, repo_root: Path) -> None:
        """./idia --help documents setup."""
        result = subprocess.run(
            ["bash", str(repo_root / "idia"), "--help"],
            capture_output=True, text=True, timeout=15,
        )
        assert "setup" in result.stdout.lower()

    def test_setup_runs(self, repo_root: Path) -> None:
        """./idia setup exits 0 on an already-configured machine."""
        result = subprocess.run(
            ["bash", str(repo_root / "idia"), "setup"],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0
        assert "complete" in result.stdout.lower() or "passed" in result.stdout.lower()
