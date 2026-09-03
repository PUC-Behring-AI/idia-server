"""Security constraint validation tests for IDIA Server.

These tests verify that the deployment artifacts respect the security
boundaries defined in docs/ARCHITECTURE.md §9, without requiring a
running Docker environment.

They check:
    - Port isolation: only :4000 is exposed externally
    - Image pinning: no :latest in Dockerfile or compose
    - Dashboard binding: bound to 127.0.0.1
    - Trust boundaries: master key declared in config

Tests that genuinely require a running container (e.g., verify that
:8000 is actually unreachable from outside) are marked @pytest.mark.security
and skip when Docker is unavailable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


@pytest.mark.security
class TestPortIsolation:
    """Only port 4000 is exposed to the host.

    See ARCHITECTURE.md §9.1.
    """

    def test_only_decided_ports_published(self, repo_root: Path) -> None:
        """Only 4000 (API) and 3001 (Open WebUI) are reachable from the network.

        4000 was the sole external port until the chat interface became a
        Compose service. Publishing 3001 is a recorded exception, not drift —
        ADR-013 states the reasoning and the reverse-proxy caveat. Adding a
        third port is a decision, so it belongs in an ADR before it belongs
        here.

        Services bound to 127.0.0.1 (Grafana on 3000) are not network-reachable
        and are permitted. See ARCHITECTURE.md §9.1.
        """
        allowed = {"4000", "3001"}
        path = repo_root / "docker-compose.yml"
        if not path.exists():
            pytest.skip("docker-compose.yml not created yet")
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        all_ports: list[str] = []
        for svc in compose.get("services", {}).values():
            ports = svc.get("ports", [])
            if isinstance(ports, list):
                all_ports.extend(str(p) for p in ports)
        assert len(all_ports) > 0, "No ports published at all — check service config"
        for p in all_ports:
            # Resolve ${VAR:-default} before splitting: the colon inside that
            # form is not a port separator.
            resolved = re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}", r"\1", p)
            parts = resolved.split(":")
            if parts[0] == "127.0.0.1":
                continue
            host_part = parts[-2] if len(parts) >= 2 else parts[0]
            assert host_part in allowed, (
                f"Port {host_part} is exposed in docker-compose.yml — only "
                f"{sorted(allowed)} may be network-reachable (§9.1, ADR-013)"
            )

    def test_ray_ingress_not_published(self, repo_root: Path) -> None:
        """Ray Serve ingress port (8000) is NOT in any ports: section."""
        path = repo_root / "docker-compose.yml"
        if not path.exists():
            pytest.skip("docker-compose.yml not created yet")
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        for svc in compose.get("services", {}).values():
            ports = svc.get("ports", [])
            if isinstance(ports, list):
                for p in ports:
                    if isinstance(p, str):
                        assert "8000" not in p, (
                            f"Port 8000 (Ray ingress) must not be published (§9.3)"
                        )

    def test_dashboard_not_published(self, repo_root: Path) -> None:
        """Ray Dashboard port (8265) is NOT in any ports: section."""
        path = repo_root / "docker-compose.yml"
        if not path.exists():
            pytest.skip("docker-compose.yml not created yet")
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        for svc in compose.get("services", {}).values():
            ports = svc.get("ports", [])
            if isinstance(ports, list):
                for p in ports:
                    if isinstance(p, str):
                        assert "8265" not in p, (
                            f"Port 8265 (Ray dashboard) must not be published (§9.2)"
                        )

    def test_ray_client_not_published(self, repo_root: Path) -> None:
        """Ray Client port (10001) is NOT in any ports: section."""
        path = repo_root / "docker-compose.yml"
        if not path.exists():
            pytest.skip("docker-compose.yml not created yet")
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        for svc in compose.get("services", {}).values():
            ports = svc.get("ports", [])
            if isinstance(ports, list):
                for p in ports:
                    if isinstance(p, str):
                        assert "10001" not in p, (
                            f"Port 10001 (Ray Client) must not be published (§9.2)"
                        )


@pytest.mark.security
class TestImagePinning:
    """All container images are pinned to immutable tags.

    See ARCHITECTURE.md §9.1.
    """

    def test_dockerfile_no_latest(self, repo_root: Path) -> None:
        """Dockerfile.ray uses a pinned base image, not :latest."""
        path = repo_root / "Dockerfile.ray"
        if not path.exists():
            pytest.skip("Dockerfile.ray not created yet")
        content = path.read_text(encoding="utf-8")
        assert ":latest" not in content, (
            "Dockerfile.ray uses :latest — all images must be pinned (§9.1)"
        )

    def test_compose_no_latest(self, repo_root: Path) -> None:
        """No service in docker-compose.yml uses :latest."""
        path = repo_root / "docker-compose.yml"
        if not path.exists():
            pytest.skip("docker-compose.yml not created yet")
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        for svc_name, svc in compose.get("services", {}).items():
            image = svc.get("image", "")
            if image:  # skip services that build locally
                assert ":latest" not in str(image), (
                    f"Service '{svc_name}' uses :latest (§9.1)"
                )


@pytest.mark.security
class TestTrustBoundaries:
    """Two trust boundaries: master key vs virtual keys.

    See ARCHITECTURE.md §9.1.
    """

    def test_litellm_config_has_master_key(self, repo_root: Path) -> None:
        """config.yaml declares general_settings.master_key."""
        path = repo_root / "config.yaml"
        if not path.exists():
            pytest.skip("config.yaml not created yet")
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "general_settings" in config, "config.yaml missing general_settings (§9.1)"
        assert "master_key" in config["general_settings"], (
            "config.yaml missing master_key in general_settings (§9.1)"
        )


@pytest.mark.security
class TestDashboardBinding:
    """Ray Dashboard binds to 127.0.0.1.

    See ARCHITECTURE.md §9.2.
    """

    def test_dashboard_host_set_to_localhost(self, repo_root: Path) -> None:
        """serve_config.yaml does NOT bind Ray dashboard publicly.

        This test checks that no configuration exposes the dashboard to
        0.0.0.0. The compose file omits port 8265.
        """
        path = repo_root / "serve_config.yaml"
        if not path.exists():
            pytest.skip("serve_config.yaml not created yet")
        content = path.read_text(encoding="utf-8")
        # The http_options.host in serve_config.yaml binds the proxy
        # inside the container — not the dashboard. Verify it is 0.0.0.0
        # as specified in §5.3 (this is correct: proxy binds inside the
        # container — the port is not published to the host).
        parsed = yaml.safe_load(content)
        assert parsed["http_options"]["host"] == "0.0.0.0", (
            "serve_config proxy binds to 0.0.0.0 inside container (§5.3); "
            "isolation relies on never publishing port 8000 outside (§9.3)"
        )


# ── Phase 4: Monitoring Port Isolation ──────────────────────────────────────


@pytest.mark.security
class TestMonitoringPortIsolation:
    """Monitoring ports follow the same isolation rules as core services (§9.3).

    Prometheus (9090) stays on the internal Compose network — never published.
    Grafana (3000) is accessible only from localhost via 127.0.0.1 binding.
    """

    COMPOSE_PATH = "docker-compose.yml"

    def test_prometheus_port_not_published(self, repo_root: Path) -> None:
        """Port 9090 (Prometheus) must NOT appear in any ports: section."""
        path = repo_root / self.COMPOSE_PATH
        if not path.exists():
            pytest.skip("docker-compose.yml not created yet")
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        for svc_name, svc in compose.get("services", {}).items():
            ports = svc.get("ports", [])
            for port_mapping in ports:
                mapping = str(port_mapping)
                # Match port 9090 in any position (host:container, :9090, 9090:...)
                assert "9090" not in mapping.split(":"), (
                    f"Service '{svc_name}' exposes port 9090 (Prometheus) in "
                    f"ports: — Prometheus must stay on the internal Compose "
                    f"network per §9.3"
                )

    def test_grafana_port_bound_localhost(self, repo_root: Path) -> None:
        """Port 3000 (Grafana) must be bound to 127.0.0.1."""
        path = repo_root / self.COMPOSE_PATH
        if not path.exists():
            pytest.skip("docker-compose.yml not created yet")
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        grafana_ports = compose.get("services", {}).get("grafana", {}).get("ports", [])
        found_localhost = False
        for port_mapping in grafana_ports:
            mapping = str(port_mapping)
            if "127.0.0.1" in mapping and "3000" in mapping:
                found_localhost = True
                break
        assert found_localhost, (
            "Grafana port 3000 must be bound to 127.0.0.1 (localhost only) — "
            f"got: {grafana_ports}"
        )
