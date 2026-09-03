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


@pytest.mark.docs
class TestDirectoryTree:
    """The directory tree in AGENTS.md must match the filesystem.

    The tree used to live in README.md with "Phase N ✓" markers next to each
    file. The phases are over, and the marker recorded when an artefact was
    born rather than what it does — so it drifted into decoration. The tree
    belongs in the agent-facing document, and what is worth enforcing is that
    every name in it exists.
    """

    def test_listed_files_exist(self, repo_root: Path) -> None:
        agents = (repo_root / "AGENTS.md").read_text(encoding="utf-8")
        start = agents.index("## Directory Layout")
        end = agents.index("## ", start + 10)
        tree = agents[start:end]

        missing: list[str] = []
        for entry in re.findall(r"[├└]──\s+(\S+)", tree):
            name = entry.rstrip("/")
            if not name or name.startswith("←"):
                continue
            # Entries under a nested block are written relative to their parent;
            # accept a match anywhere in the tree to keep the check simple and
            # the tree readable.
            if (repo_root / name).exists():
                continue
            if list(repo_root.rglob(name)):
                continue
            missing.append(entry)

        assert not missing, f"listed in AGENTS.md but absent from disk: {missing}"

    def test_tree_has_no_phase_markers(self, repo_root: Path) -> None:
        """Phase bookkeeping is history, not structure — it belongs in the ADRs.

        Scoped to the fenced block: the prose above it explains what was
        removed and has to name it to do so.
        """
        agents = (repo_root / "AGENTS.md").read_text(encoding="utf-8")
        start = agents.index("## Directory Layout")
        end = agents.index("## ", start + 10)
        section = agents[start:end]
        fence = section.index("```")
        tree = section[fence : section.index("```", fence + 3)]
        assert "Phase " not in tree, "phase markers are back in the tree"



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
