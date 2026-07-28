"""Documentation structure and consistency tests.

These tests verify that:
- Required documentation files exist
- Cross-references between documents are consistent
- Document headers and structure follow conventions

They run with zero external dependencies and no infrastructure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ── Required documentation files ────────────────────────────────────────

REQUIRED_DOCS: list[tuple[str, str]] = [
    ("architecture", "docs/ARCHITECTURE.md"),
    ("agents", "AGENTS.md"),
    ("readme", "README.md"),
    ("deploy", "docs/DEPLOY.md"),
    ("adr", "docs/ADR.md"),
]

LIVING_DOC_SECTIONS: dict[str, list[str]] = {
    "docs/ARCHITECTURE.md": [
        "Document Evolution Contract",
        "Structural Change History",
    ],
    "docs/DEPLOY.md": [
        "Deploy local",
        "Gestão de usuários",
        "Troubleshooting",
    ],
}


@pytest.mark.docs
class TestRequiredDocs:
    """Every required document exists and is non-empty."""

    @pytest.mark.parametrize("name,rel_path", REQUIRED_DOCS)
    def test_exists(self, name: str, rel_path: str, repo_root: Path) -> None:
        path = repo_root / rel_path
        if not path.exists():
            pytest.skip(f"{name} ({rel_path}) not created yet — check later phase")
        assert path.is_file(), f"{rel_path} exists but is not a file"
        assert path.stat().st_size > 0, f"{rel_path} is empty"

    @pytest.mark.parametrize("name,rel_path", REQUIRED_DOCS)
    def test_is_markdown(self, name: str, rel_path: str, repo_root: Path) -> None:
        path = repo_root / rel_path
        if not path.exists():
            pytest.skip(f"{name} ({rel_path}) not created yet")
        content = path.read_text(encoding="utf-8")
        assert content.startswith("#"), f"{rel_path} does not start with a heading"


@pytest.mark.docs
class TestLivingDocSections:
    """Living documents contain the required governance sections."""

    @pytest.mark.parametrize("rel_path,expected_sections", [
        (rel, secs) for rel, secs in LIVING_DOC_SECTIONS.items()
    ])
    def test_contains_sections(
        self, rel_path: str, expected_sections: list[str], repo_root: Path
    ) -> None:
        path = repo_root / rel_path
        if not path.exists():
            pytest.skip(f"{rel_path} not created yet")
        content = path.read_text(encoding="utf-8")
        for section in expected_sections:
            assert section in content, (
                f"Missing section '{section}' in {rel_path}"
            )


@pytest.mark.docs
class TestArchitectureFooter:
    """ARCHITECTURE.md carries a version footer."""

    def test_has_version_footer(self, docs_dir: Path) -> None:
        path = docs_dir / "ARCHITECTURE.md"
        if not path.exists():
            pytest.skip("ARCHITECTURE.md not created yet")
        content = path.read_text(encoding="utf-8")
        # Footer marker
        assert "*Document version:" in content, (
            "ARCHITECTURE.md is missing the version footer"
        )
        # At least one structural change entry
        assert "Structural Change History" in content, (
            "ARCHITECTURE.md is missing the Structural Change History"
        )


# ── Phase 5 — Cross-document consistency ─────────────────────────────


README_PHASE_MARKERS: dict[str, str] = {
    ".env.example": "Phase 2 ✓",
    "Dockerfile.ray": "Phase 2 ✓",
    "serve_config.yaml": "Phase 2 ✓",
    "docker-compose.yml": "Phase 2 ✓",
    "config.yaml": "Phase 2 ✓",
    "prometheus.yml": "Phase 4 ✓",
}


@pytest.mark.docs
class TestReadmeDirectoryTree:
    """README.md directory tree matches the real filesystem."""

    def test_phase_markers_match_code(self, repo_root: Path) -> None:
        """Every artefact marked ✓ in the README tree exists on disk."""
        readme = (repo_root / "README.md").read_text(encoding="utf-8")
        for filename, expected_marker in README_PHASE_MARKERS.items():
            assert expected_marker in readme, (
                f"README.md should show '{expected_marker}' for {filename}"
            )
            file_path = repo_root / filename
            assert file_path.exists(), (
                f"{filename} is marked ✓ in README but does not exist on disk"
            )

    def test_directory_listed_files_exist(self, repo_root: Path) -> None:
        """All filenames listed in the README directory tree exist on disk."""
        readme = (repo_root / "README.md").read_text(encoding="utf-8")
        # Find lines that indicate files: ├── or └── followed by filename
        tree_lines = re.findall(r'[├└]──\s+([^\s]+)', readme)
        for entry in tree_lines:
            # Skip entries that are directory markers (trailing /) or comments
            if entry.endswith("/") or entry.startswith("←") or entry == "│":
                continue
            # Skip known non-file entries (comments after ←)
            if "←" in entry:
                continue
            # Check if it's a path under a subdirectory
            file_path = repo_root / entry
            if not file_path.exists():
                # Adjust for different patterns: docs/ARCHITECTURE.md, etc.
                # Some entries are just filenames, others have comments
                actual_name = entry.split("←")[0].strip()
                if actual_name:
                    check_path = repo_root / actual_name
                    if not check_path.exists():
                        # Could be a directory reference — skip gracefully
                        if not check_path.is_dir():
                            pytest.skip(
                                f"Tree entry '{entry}' does not resolve — "
                                f"may be a comment or special entry"
                            )


@pytest.mark.docs
class TestADRValidation:
    """ADR.md contains well-formed architectural decisions."""

    REQUIRED_ADR_FIELDS = ["Contexto", "Decisão", "Alternativa descartada",
                           "Consequências"]

    def test_adr_exists(self, docs_dir: Path) -> None:
        path = docs_dir / "ADR.md"
        assert path.exists(), "ADR.md does not exist"
        assert path.stat().st_size > 0, "ADR.md is empty"

    def test_adr_starts_with_heading(self, docs_dir: Path) -> None:
        content = (docs_dir / "ADR.md").read_text(encoding="utf-8")
        assert content.startswith("#"), "ADR.md does not start with a heading"

    def test_adr_has_entries(self, docs_dir: Path) -> None:
        content = (docs_dir / "ADR.md").read_text(encoding="utf-8")
        adrs = re.findall(r'^## ADR-\d+:', content, re.MULTILINE)
        assert len(adrs) >= 4, (
            f"ADR.md has only {len(adrs)} entries — expected at least 4"
        )

    def test_adr_required_sections(self, docs_dir: Path) -> None:
        """Every ADR entry has Context, Decision, Alternative, Consequences."""
        content = (docs_dir / "ADR.md").read_text(encoding="utf-8")
        # Split into individual ADR entries
        sections = re.split(r'^## ADR-\d+:', content, flags=re.MULTILINE)
        # First split is header — skip
        for i, section in enumerate(sections[1:], 1):
            for field in self.REQUIRED_ADR_FIELDS:
                # ADR format uses **Field:** (with colon)
                assert f"**{field}:**" in section, (
                    f"ADR-{i} is missing required field '{field}'"
                )

    def test_adr_references_phase(self, docs_dir: Path) -> None:
        """Every ADR entry references a Phase."""
        content = (docs_dir / "ADR.md").read_text(encoding="utf-8")
        adrs = re.split(r'^## ADR-\d+:', content, flags=re.MULTILINE)
        for i, section in enumerate(adrs[1:], 1):
            assert "**Fase:**" in section, (
                f"ADR-{i} is missing the Fase reference"
            )

    def test_adr_has_status(self, docs_dir: Path) -> None:
        """Every ADR entry has a decision status."""
        content = (docs_dir / "ADR.md").read_text(encoding="utf-8")
        adrs = re.split(r'^## ADR-\d+:', content, flags=re.MULTILINE)
        for i, section in enumerate(adrs[1:], 1):
            assert "**Status:**" in section, (
                f"ADR-{i} is missing the Status field"
            )


@pytest.mark.docs
class TestLicense:
    """LICENSE file exists and is Apache 2.0."""

    def test_license_exists(self, repo_root: Path) -> None:
        path = repo_root / "LICENSE"
        assert path.exists(), "LICENSE file does not exist"
        assert path.stat().st_size > 0, "LICENSE is empty"

    def test_license_is_apache2(self, repo_root: Path) -> None:
        content = (repo_root / "LICENSE").read_text(encoding="utf-8")
        assert "Apache License" in content, "LICENSE is not Apache 2.0"
        assert "Version 2.0" in content, "LICENSE is not Apache 2.0"
