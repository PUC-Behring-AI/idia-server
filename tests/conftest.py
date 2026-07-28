"""Shared fixtures for the IDIA Server test suite.

All test categories (docs, config, integration, security) share
the repo_root fixture. Category-specific fixtures live in the
corresponding test module.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def repo_root() -> Path:
    """Absolute path to the repository root."""
    return Path(__file__).parent.parent.resolve()


@pytest.fixture
def docs_dir(repo_root: Path) -> Path:
    """Path to the docs/ directory."""
    return repo_root / "docs"


@pytest.fixture
def config_files(repo_root: Path) -> dict[str, Path]:
    """Map of logical config names to their expected file paths.

    Files that do not yet exist (to be created in later phases) are
    still listed so tests can clearly distinguish "file absent" from
    "file missing". Tests SHOULD skip with pytest.skip() rather than
    fail when a Phase 2+ file is not yet present.
    """
    return {
        "agents": repo_root / "AGENTS.md",
        "dockerfile_ray": repo_root / "Dockerfile.ray",
        "serve_config": repo_root / "serve_config.yaml",
        "docker_compose": repo_root / "docker-compose.yml",
        "config_litellm": repo_root / "config.yaml",
        "prometheus": repo_root / "prometheus.yml",
        "env_example": repo_root / ".env.example",
    }
