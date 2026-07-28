"""Configuration file schema validation tests.

These tests validate that infrastructure configuration files
(Dockerfile.ray, serve_config.yaml, docker-compose.yml, config.yaml,
.env.example, prometheus.yml) conform to the structure
defined in docs/ARCHITECTURE.md.

Each test skips gracefully if the target file has not been created
yet (later phase), so this module is safe to run from Phase 1 onward.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


# ── Helpers ─────────────────────────────────────────────────────────────

def _read_yaml(path: Path) -> dict | None:
    """Return parsed YAML, or None if file is absent."""
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    return yaml.safe_load(raw)


def _has_keys(obj: dict, *keys: str) -> bool:
    """Check that *keys exist at the top level of *obj*."""
    return all(k in obj for k in keys)


# ── serve_config.yaml (§5.3) ────────────────────────────────────────────

SERVE_CONFIG_REQUIRED_KEYS = [
    "proxy_location",
    "http_options",
    "applications",
]


@pytest.mark.config
class TestServeConfig:
    """serve_config.yaml structure per §5.3 of the architecture."""

    @pytest.fixture
    def config(self, config_files: dict[str, Path]) -> dict | None:
        return _read_yaml(config_files["serve_config"])

    def test_exists(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("serve_config.yaml not created yet (Phase 2)")
        assert isinstance(config, dict)

    def test_has_required_keys(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("serve_config.yaml not created yet")
        for key in SERVE_CONFIG_REQUIRED_KEYS:
            assert key in config, f"Missing required key: {key}"

    def test_proxy_location(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("serve_config.yaml not created yet")
        assert config.get("proxy_location") == "EveryNode"

    def test_http_options_host(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("serve_config.yaml not created yet")
        opts = config.get("http_options", {})
        # Per §9.2: binds 0.0.0.0 inside the container only
        assert opts.get("host") == "0.0.0.0"

    def test_http_options_port(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("serve_config.yaml not created yet")
        opts = config.get("http_options", {})
        assert opts.get("port") == 8000

    def test_applications_is_list(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("serve_config.yaml not created yet")
        apps = config.get("applications", [])
        assert isinstance(apps, list), "applications must be a list"
        assert len(apps) > 0, "at least one application required"

    def test_first_app_has_route_prefix(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("serve_config.yaml not created yet")
        apps = config.get("applications", [])
        if not apps:
            pytest.skip("no applications defined yet")
        assert "/" in apps[0].get("route_prefix", "")


# ── docker-compose.yml (§5.4, §10.2) ────────────────────────────────────

COMPOSE_REQUIRED_SERVICES = ["ray-head", "litellm", "prometheus", "grafana", "dcgm-exporter"]


@pytest.mark.config
class TestDockerCompose:
    """docker-compose.yml structure per §5.4 and §10.2."""

    @pytest.fixture
    def config(self, config_files: dict[str, Path]) -> dict | None:
        return _read_yaml(config_files["docker_compose"])

    def test_exists(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("docker-compose.yml not created yet (Phase 2)")
        assert isinstance(config, dict)

    def test_has_required_services(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("docker-compose.yml not created yet")
        services = config.get("services", {})
        for svc in COMPOSE_REQUIRED_SERVICES:
            assert svc in services, f"Missing required service: {svc}"

    def test_ray_head_ipc(self, config: dict | None) -> None:
        """Ray requires ipc=host for shared-memory multiprocessing."""
        if config is None:
            pytest.skip("docker-compose.yml not created yet")
        svc = config.get("services", {}).get("ray-head", {})
        assert svc.get("ipc") == "host", "ray-head needs ipc: host"

    def test_ray_head_shm_size(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("docker-compose.yml not created yet")
        svc = config.get("services", {}).get("ray-head", {})
        assert "shm_size" in svc, "ray-head needs shm_size set"


# ── config.yaml (LiteLLM) (§4.3) ───────────────────────────────────────

LITELLM_REQUIRED_KEYS = ["model_list", "general_settings"]


@pytest.mark.config
class TestLiteLLMConfig:
    """config.yaml (LiteLLM) structure per §4.3."""

    @pytest.fixture
    def config(self, config_files: dict[str, Path]) -> dict | None:
        return _read_yaml(config_files["config_litellm"])

    def test_exists(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("config.yaml not created yet (Phase 2)")
        assert isinstance(config, dict)

    def test_has_required_keys(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("config.yaml not created yet")
        for key in LITELLM_REQUIRED_KEYS:
            assert key in config, f"Missing required key: {key}"

    def test_model_list_has_entries(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("config.yaml not created yet")
        model_list = config.get("model_list", [])
        assert len(model_list) > 0, "model_list must have at least one entry"

    def test_general_settings_has_master_key(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("config.yaml not created yet")
        gs = config.get("general_settings", {})
        assert "master_key" in gs, "general_settings must declare master_key source"


# ── prometheus.yml (§10.2) ──────────────────────────────────────────────

PROMETHEUS_REQUIRED_KEYS = ["global", "scrape_configs"]

# Expected scrape target addresses from docker-compose service names
EXPECTED_TARGETS = ["ray-head:8080", "litellm:4000", "dcgm-exporter:9400"]


@pytest.mark.config
class TestPrometheusConfig:
    """prometheus.yml structure per §10.2."""

    @pytest.fixture
    def config(self, config_files: dict[str, Path]) -> dict | None:
        return _read_yaml(config_files["prometheus"])

    def test_exists(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("prometheus.yml not created yet (Phase 4)")
        assert isinstance(config, dict)

    def test_has_required_keys(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("prometheus.yml not created yet")
        for key in PROMETHEUS_REQUIRED_KEYS:
            assert key in config, f"Missing required key: {key}"

    def test_scrape_configs_list(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("prometheus.yml not created yet")
        scrape_configs = config.get("scrape_configs", [])
        assert isinstance(scrape_configs, list)
        assert len(scrape_configs) > 0

    # ── Phase 4 extended tests ────────────────────────────────────────────

    def test_scrape_interval_15s(self, config: dict | None) -> None:
        """Global scrape interval matches the architecture default."""
        if config is None:
            pytest.skip("prometheus.yml not created yet (Phase 4)")
        interval = config.get("global", {}).get("scrape_interval")
        assert interval == "15s", (
            f"Expected scrape_interval=15s, got {interval!r} (§10.2)"
        )

    def test_targets_match_architecture(self, config: dict | None) -> None:
        """Scrape targets must include ray-head:8080 and litellm:4000."""
        if config is None:
            pytest.skip("prometheus.yml not created yet (Phase 4)")
        jobs = config.get("scrape_configs", [])
        found_targets: set[str] = set()
        for job in jobs:
            for group in job.get("static_configs", []):
                for target in group.get("targets", []):
                    found_targets.add(target)
        for expected in EXPECTED_TARGETS:
            assert expected in found_targets, (
                f"Expected scrape target {expected!r} not found in "
                f"scrape_configs — got {sorted(found_targets)}"
            )

    def test_no_alerting_rules(self, config: dict | None) -> None:
        """Alerting rules are delegated to Grafana, not Prometheus."""
        if config is None:
            pytest.skip("prometheus.yml not created yet (Phase 4)")
        rule_files = config.get("rule_files", None)
        assert rule_files is None or rule_files == [], (
            "Prometheus-level alerting rules should not be configured — "
            "alerts are delegated to Grafana (§10.3)"
        )


# ── grafana/datasources/datasource.yml (§10.2) ─────────────────────────────

GRAFANA_DATASOURCE_KEY = "datasources"


@pytest.mark.config
class TestGrafanaDatasourceConfig:
    """grafana/datasources/datasource.yml — Prometheus datasource provisioning."""

    PATH = "grafana/datasources/datasource.yml"

    @pytest.fixture
    def datasource(self, repo_root: Path) -> dict | None:
        path = repo_root / self.PATH
        if not path.exists():
            pytest.skip("grafana/datasources not created yet (Phase 4)")
        with open(path, encoding="utf-8") as f:
            parsed = yaml.safe_load(f)
        return parsed

    def test_datasource_file_exists(self, repo_root: Path) -> None:
        path = repo_root / self.PATH
        assert path.exists(), (
            f"{self.PATH} must exist for Grafana datasource provisioning"
        )

    def test_has_datasources_key(self, datasource: dict | None) -> None:
        if datasource is None:
            pytest.skip("grafana/datasources not created yet")
        assert GRAFANA_DATASOURCE_KEY in datasource, (
            f"Missing key: {GRAFANA_DATASOURCE_KEY}"
        )

    def test_datasource_url(self, datasource: dict | None) -> None:
        """Datasource URL must point to the Prometheus service."""
        if datasource is None:
            pytest.skip("grafana/datasources not created yet")
        ds_list = datasource.get(GRAFANA_DATASOURCE_KEY, [])
        assert len(ds_list) >= 1, "At least one datasource must be configured"
        url = ds_list[0].get("url", "")
        assert url == "http://prometheus:9090", (
            f"Expected datasource url=http://prometheus:9090, got {url!r}"
        )

    def test_datasource_is_default(self, datasource: dict | None) -> None:
        if datasource is None:
            pytest.skip("grafana/datasources not created yet")
        ds_list = datasource.get(GRAFANA_DATASOURCE_KEY, [])
        assert ds_list[0].get("isDefault") is True, (
            "Datasource must be configured as default"
        )

    def test_datasource_access_proxy(self, datasource: dict | None) -> None:
        if datasource is None:
            pytest.skip("grafana/datasources not created yet")
        ds_list = datasource.get(GRAFANA_DATASOURCE_KEY, [])
        assert ds_list[0].get("access") == "proxy", (
            "Datasource access mode should be 'proxy' — Grafana proxies "
            "requests to Prometheus on the internal network"
        )

    def test_datasource_type_prometheus(self, datasource: dict | None) -> None:
        if datasource is None:
            pytest.skip("grafana/datasources not created yet")
        ds_list = datasource.get(GRAFANA_DATASOURCE_KEY, [])
        assert ds_list[0].get("type") == "prometheus", (
            "Datasource type must be 'prometheus'"
        )


# ── .env.example ────────────────────────────────────────────────────────

REQUIRED_ENV_VARS = ["HF_TOKEN", "LITELLM_MASTER_KEY", "MODEL_ID", "MODEL_SOURCE"]


@pytest.mark.config
class TestEnvExample:
    """.env.example documents all required env vars."""

    def test_declares_required_vars(self, config_files: dict[str, Path]) -> None:
        path = config_files["env_example"]
        if not path.exists():
            pytest.skip(".env.example not created yet (Phase 2)")
        content = path.read_text(encoding="utf-8")
        for var in REQUIRED_ENV_VARS:
            assert var in content, f".env.example missing required var: {var}"
