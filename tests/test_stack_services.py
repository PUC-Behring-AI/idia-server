"""Tests for the service topology declared in docker-compose.yml.

Two things get checked here that no other test covers:

  * the gateway can actually issue a virtual key — meaning LiteLLM has a
    database wired to it (ADR-012). Without that, every multi-user claim in
    ARCHITECTURE.md §1 is false;
  * the set of ports reachable from the network is exactly the set that was
    decided on. A new service that publishes a port fails this suite rather
    than being discovered by a scan.

Nothing here starts a container: the file is parsed as YAML.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

# Reachable from the network, by decision. Grafana is bound to loopback and
# therefore is not in this set — it is checked separately.
EXPECTED_PUBLIC_PORTS = {"4000", "3001"}

# Never publishable: Ray ingress, Ray dashboard, Ray client, Prometheus,
# PostgreSQL, DCGM.
FORBIDDEN_PORTS = {"8000", "8265", "10001", "9090", "5432", "9400"}


@pytest.fixture
def compose(repo_root: Path) -> dict:
    return yaml.safe_load((repo_root / "docker-compose.yml").read_text(encoding="utf-8"))


def published(compose: dict) -> list[tuple[str, str]]:
    """(service, port spec) for every published port in the file."""
    out = []
    for name, svc in compose["services"].items():
        for spec in svc.get("ports", []) or []:
            out.append((name, str(spec)))
    return out


_COMPOSE_DEFAULT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}")


def host_port(spec: str) -> str:
    """Host-side port from a compose port spec.

    Resolves ``${VAR:-default}`` to its default first — the colon inside that
    form would otherwise be read as a port separator, so ``${OWUI_PORT:-3001}``
    parses as the host port ``-3001}``.
    """
    resolved = _COMPOSE_DEFAULT.sub(r"\1", spec)
    parts = resolved.split(":")
    return parts[-2] if len(parts) >= 2 else parts[0]


# ── Gateway database (ADR-012) ──────────────────────────────────────────


class TestGatewayDatabase:
    """LiteLLM without a database cannot issue a single virtual key."""

    def test_postgres_service_exists(self, compose: dict) -> None:
        assert "postgres" in compose["services"]

    def test_litellm_receives_database_url(self, compose: dict) -> None:
        env = "\n".join(compose["services"]["litellm"]["environment"])
        assert "DATABASE_URL=" in env, "LiteLLM has no DATABASE_URL — /key/generate cannot persist"
        assert "postgresql://" in env

    def test_litellm_waits_for_a_healthy_database(self, compose: dict) -> None:
        """Starting before Postgres accepts connections fails the first migration."""
        depends = compose["services"]["litellm"]["depends_on"]
        assert "postgres" in depends
        assert depends["postgres"]["condition"] == "service_healthy"

    def test_postgres_has_a_healthcheck(self, compose: dict) -> None:
        assert "healthcheck" in compose["services"]["postgres"]

    def test_postgres_data_is_a_named_volume(self, compose: dict) -> None:
        """The keys of every user and the whole spend history live here."""
        assert "postgres_data" in compose["volumes"]
        mounts = compose["services"]["postgres"]["volumes"]
        assert any("postgres_data:" in m for m in mounts)

    def test_password_has_no_silent_default(self, repo_root: Path) -> None:
        raw = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
        assert "${POSTGRES_PASSWORD:?" in raw, (
            "must refuse to start rather than fall back to a blank password"
        )


# ── Chat interface (ADR-013) ────────────────────────────────────────────


class TestWebInterface:
    def test_open_webui_is_a_service(self, compose: dict) -> None:
        assert "open-webui" in compose["services"]

    def test_container_name_is_fixed(self, compose: dict) -> None:
        """colleague.sh reaches into this container by name (ADR-009)."""
        name = compose["services"]["open-webui"].get("container_name", "")
        assert "idia-webui" in name

    def test_waits_for_a_healthy_gateway(self, compose: dict) -> None:
        depends = compose["services"]["open-webui"]["depends_on"]
        assert depends["litellm"]["condition"] == "service_healthy"

    def test_signup_is_closed(self, compose: dict) -> None:
        """A self-registered account has no key and no grants — an empty dropdown."""
        env = "\n".join(compose["services"]["open-webui"]["environment"])
        assert "ENABLE_SIGNUP=false" in env

    def test_data_volume_is_declared(self, compose: dict) -> None:
        assert "webui_data" in compose["volumes"]

    def test_no_literal_discovery_key(self, repo_root: Path) -> None:
        raw = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
        assert "OPENAI_API_KEY=${OWUI_DISCOVERY_KEY" in raw
        assert "sk-idia" not in raw


# ── Port surface ────────────────────────────────────────────────────────


@pytest.mark.security
class TestPortSurface:
    def test_public_ports_are_exactly_the_decided_set(self, compose: dict) -> None:
        reachable = {
            host_port(spec)
            for _, spec in published(compose)
            if not spec.startswith("127.0.0.1")
        }
        assert reachable == EXPECTED_PUBLIC_PORTS, (
            f"network-reachable ports changed: {reachable} != {EXPECTED_PUBLIC_PORTS}"
        )

    def test_no_forbidden_port_is_published(self, compose: dict) -> None:
        for service, spec in published(compose):
            assert host_port(spec) not in FORBIDDEN_PORTS, (
                f"{service} publishes {spec} — see ARCHITECTURE.md §9.2"
            )

    def test_postgres_is_not_published(self, compose: dict) -> None:
        assert not compose["services"]["postgres"].get("ports")

    def test_grafana_stays_on_loopback(self, compose: dict) -> None:
        for spec in compose["services"]["grafana"]["ports"]:
            assert str(spec).startswith("127.0.0.1"), "Grafana must not be network-reachable"


# ── HuggingFace cache path ──────────────────────────────────────────────


class TestModelCachePath:
    """The Ray base image runs as UID 1000, which cannot write to /root."""

    def test_cache_is_not_under_root(self, compose: dict) -> None:
        mounts = compose["services"]["ray-head"]["volumes"]
        offenders = [m for m in mounts if "/root/" in str(m)]
        assert not offenders, f"cache mounted where the ray user cannot write: {offenders}"

    def test_hf_home_matches_the_mount(self, compose: dict) -> None:
        svc = compose["services"]["ray-head"]
        env = "\n".join(svc["environment"])
        assert "HF_HOME=/home/ray/.cache/huggingface" in env
        assert any("/home/ray/.cache/huggingface" in str(m) for m in svc["volumes"])


# ── .env.example ────────────────────────────────────────────────────────


class TestEnvExampleDocumentsRequirements:
    def test_new_required_vars_are_documented(self, repo_root: Path) -> None:
        content = (repo_root / ".env.example").read_text(encoding="utf-8")
        for var in ("POSTGRES_PASSWORD", "UI_USERNAME", "UI_PASSWORD"):
            assert var in content, f".env.example does not document {var}"

    def test_secrets_ship_empty(self, repo_root: Path) -> None:
        """A placeholder password is a password somebody keeps."""
        content = (repo_root / ".env.example").read_text(encoding="utf-8")
        for var in ("POSTGRES_PASSWORD", "UI_PASSWORD"):
            for line in content.splitlines():
                if line.startswith(f"{var}="):
                    assert line == f"{var}=", f"{var} ships with a value"
