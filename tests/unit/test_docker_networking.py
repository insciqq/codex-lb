from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.config.settings import Settings
from app.core.server import POST_DRAIN_CLEANUP_TIMEOUT_SECONDS

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("compose_name", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_stock_compose_uses_user_defined_default_bridge(compose_name: str) -> None:
    compose: dict[str, Any] = yaml.safe_load((_REPO_ROOT / compose_name).read_text(encoding="utf-8"))

    assert compose["networks"]["default"] == {"driver": "bridge"}
    for service in compose["services"].values():
        assert service.get("network_mode") != "bridge"
        assert "dns" not in service


def test_standalone_docker_examples_use_named_bridge() -> None:
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    standalone_launches = readme.count("docker run -d --name codex-lb")
    portable_launches = readme.count("--network codex-lb-net")
    host_network_launches = readme.count("--network host")

    assert standalone_launches > 0
    assert portable_launches + host_network_launches == standalone_launches
    assert (
        readme.count("docker network inspect codex-lb-net >/dev/null 2>&1 || docker network create codex-lb-net")
        == portable_launches
    )
    assert "--dns " not in readme


def test_docker_docs_basic_run_uses_named_bridge() -> None:
    docker_docs = (_REPO_ROOT / "docs/deployment/docker.md").read_text(encoding="utf-8")
    basic_run = docker_docs.split("## Basic run", 1)[1].split("## Switching Wi-Fi or other networks", 1)[0]

    assert "docker network inspect codex-lb-net >/dev/null 2>&1 || docker network create codex-lb-net" in basic_run
    assert "--network codex-lb-net" in basic_run
    assert "--dns " not in basic_run


def test_network_switching_guidance_is_cross_platform_and_approachable() -> None:
    docker_docs = (_REPO_ROOT / "docs/deployment/docker.md").read_text(encoding="utf-8")
    switching_section = docker_docs.split("## Switching Wi-Fi or other networks", 1)[1].split("## Docker Compose", 1)[0]

    assert "home Wi-Fi to a phone hotspot" in switching_section
    assert "Linux, macOS, and Windows" in switching_section
    assert "uvx codex-lb" in switching_section
    assert "Docker Desktop on macOS or Windows" in switching_section
    assert "Docker Desktop 4.34 and later" in switching_section
    assert "not been verified as a reliable fix" in switching_section
    assert "--network host" in switching_section
    assert "stable resolver address" in switching_section
    assert "127.0.0.53" in switching_section
    assert "supplied by Wi-Fi or other DHCP" in switching_section
    assert " -p " not in switching_section
    assert "DNS server from the previous network" in switching_section


def test_running_container_resolver_runbook_uses_bridge_scoped_systemd_listener() -> None:
    context = (_REPO_ROOT / "openspec/specs/deployment-networking/context.md").read_text(encoding="utf-8")

    assert "DNSStubListenerExtra=%s" in context
    assert "docker exec --user 0 codex-lb" in context
    assert "without restarting codex-lb" in context
    assert "rather than `0.0.0.0`" in context


def _duration_seconds(value: str) -> float:
    units = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    for suffix in ("ms", "s", "m", "h"):
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * units[suffix]
    raise AssertionError(f"unsupported duration {value!r}")


# deployment-installation: drain timeout + post-drain cleanup reserve + 5 s.
_COMPOSE_STOP_GRACE_MARGIN_SECONDS = 5


def _compose_minimum_stop_grace_seconds() -> float:
    drain_timeout = Settings.model_fields["shutdown_drain_timeout_seconds"].default
    return drain_timeout + POST_DRAIN_CLEANUP_TIMEOUT_SECONDS + _COMPOSE_STOP_GRACE_MARGIN_SECONDS


@pytest.mark.parametrize("compose_name", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_stock_compose_stop_grace_covers_drain_and_cleanup(compose_name: str) -> None:
    """Docker's default 10 s stop grace would SIGKILL the app drain mid-stream."""
    compose: dict[str, Any] = yaml.safe_load((_REPO_ROOT / compose_name).read_text(encoding="utf-8"))

    grace = _duration_seconds(compose["services"]["server"]["stop_grace_period"])

    assert grace >= _compose_minimum_stop_grace_seconds()


def test_docker_guide_states_the_compose_stop_grace_minimum() -> None:
    guide = " ".join((_REPO_ROOT / "docs/deployment/docker.md").read_text(encoding="utf-8").split())
    drain_timeout = Settings.model_fields["shutdown_drain_timeout_seconds"].default
    extra = int(_compose_minimum_stop_grace_seconds() - drain_timeout)
    minimum = int(_compose_minimum_stop_grace_seconds())

    assert f"at least the drain timeout plus {extra} seconds" in guide
    assert f"which is {minimum} seconds at the default" in guide
