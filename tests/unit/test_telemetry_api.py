from __future__ import annotations

import asyncio
import logging
import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.config.settings import get_settings
from app.db.models import DashboardSettings
from app.db.session import SessionLocal

pytestmark = pytest.mark.unit


def _use_unset_upstream_default(monkeypatch) -> None:
    """Restore upstream's unset ``telemetry_enabled`` (``None``) for this test.

    The fork defaults the setting to ``False`` and no environment value yields
    ``None``, but the undecided-means-active code path stays covered here.
    """
    monkeypatch.delenv("CODEX_LB_TELEMETRY_ENABLED", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "telemetry_enabled", None)


@pytest.fixture(autouse=True)
def opt_out_sender(monkeypatch):
    sender = Mock()
    sender.send_opt_out = AsyncMock()
    factory = Mock(return_value=sender)
    monkeypatch.setattr("app.modules.telemetry.api.TelemetrySender", factory)
    return sender


@pytest.mark.asyncio
async def test_consent_api_get_preview_and_put_persists_without_restart(
    async_client,
    monkeypatch,
    opt_out_sender,
) -> None:
    _use_unset_upstream_default(monkeypatch)

    response = await async_client.get("/api/settings/telemetry")
    assert response.status_code == 200
    initial = response.json()
    assert initial["state"] == "undecided"
    assert initial["source"] == "default"
    assert initial["active"] is True
    assert set(initial["preview"]) == {"instance_id", "metrics", "timestamp"}
    assert initial["preview"]["metrics"]["schema_version"] == 1
    assert initial["preview"]["metrics"]["consent"] == "undecided"
    assert initial["preview"]["instance_id"] == initial["preview"]["metrics"]["instance_id"]

    response = await async_client.put("/api/settings/telemetry", json={"enabled": False})
    assert response.status_code == 200
    disabled = response.json()
    assert disabled["state"] == "disabled"
    assert disabled["source"] == "persisted"
    assert disabled["active"] is False
    assert disabled["preview"] is None
    await asyncio.sleep(0)
    opt_out_sender.send_opt_out.assert_awaited_once()

    builder = Mock(side_effect=AssertionError("decided consent must not build a preview"))
    monkeypatch.setattr("app.modules.telemetry.api.TelemetrySnapshotBuilder", builder)
    response = await async_client.get("/api/settings/telemetry")
    assert response.status_code == 200
    assert response.json()["state"] == "disabled"
    assert response.json()["preview"] is None
    builder.assert_not_called()


@pytest.mark.asyncio
async def test_consent_api_builds_decided_preview_only_when_requested(
    async_client,
    monkeypatch,
) -> None:
    monkeypatch.delenv("CODEX_LB_TELEMETRY_ENABLED", raising=False)
    get_settings.cache_clear()
    await async_client.put("/api/settings/telemetry", json={"enabled": False})

    response = await async_client.get("/api/settings/telemetry?include_preview=true")

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "disabled"
    assert payload["preview"]["instance_id"] == payload["preview"]["metrics"]["instance_id"]
    assert payload["preview"]["metrics"]["consent"] == "enabled"


@pytest.mark.asyncio
@pytest.mark.parametrize("env_value", [None, "false"])
async def test_inactive_preview_shows_enabled_consent_without_minting_an_identity(
    async_client,
    monkeypatch,
    env_value: str | None,
) -> None:
    # None is the fork default (telemetry_enabled=False); "false" sets it explicitly.
    if env_value is None:
        monkeypatch.delenv("CODEX_LB_TELEMETRY_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CODEX_LB_TELEMETRY_ENABLED", env_value)
    get_settings.cache_clear()

    first = await async_client.get("/api/settings/telemetry?include_preview=true")
    second = await async_client.get("/api/settings/telemetry?include_preview=true")

    assert first.status_code == 200
    payload = first.json()
    assert (payload["state"], payload["source"], payload["active"]) == ("disabled", "env", False)
    # Both opt-in paths (Settings enable, CODEX_LB_TELEMETRY_ENABLED=true) send "enabled".
    assert payload["preview"]["metrics"]["consent"] == "enabled"
    instance_id = payload["preview"]["instance_id"]
    assert payload["preview"]["metrics"]["instance_id"] == instance_id
    assert uuid.UUID(instance_id).version == 4
    # A throwaway id per preview: nothing was stored to reuse.
    assert second.json()["preview"]["instance_id"] != instance_id
    async with SessionLocal() as session:
        row = await session.get(DashboardSettings, 1)
    assert row is not None
    assert row.telemetry_instance_id is None
    assert row.telemetry_private_key_encrypted is None


@pytest.mark.asyncio
async def test_fork_default_reports_env_disabled_without_dialog_preview(async_client, monkeypatch) -> None:
    monkeypatch.delenv("CODEX_LB_TELEMETRY_ENABLED", raising=False)
    get_settings.cache_clear()
    assert get_settings().telemetry_enabled is False

    builder = Mock(side_effect=AssertionError("the fork default must not build a dialog preview"))
    monkeypatch.setattr("app.modules.telemetry.api.TelemetrySnapshotBuilder", builder)
    response = await async_client.get("/api/settings/telemetry")

    assert response.status_code == 200
    payload = response.json()
    # source "env" with no preview keeps the dashboard consent dialog closed.
    assert payload == {"state": "disabled", "source": "env", "active": False, "preview": None}
    builder.assert_not_called()


@pytest.mark.asyncio
async def test_consent_api_env_override_wins_and_suppresses_undecided_state(async_client, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_LB_TELEMETRY_ENABLED", "true")
    get_settings.cache_clear()

    builder = Mock(side_effect=AssertionError("environment override must not build a preview"))
    monkeypatch.setattr("app.modules.telemetry.api.TelemetrySnapshotBuilder", builder)
    response = await async_client.get("/api/settings/telemetry")

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "enabled"
    assert payload["source"] == "env"
    assert payload["active"] is True
    assert payload["preview"] is None
    builder.assert_not_called()


@pytest.mark.asyncio
async def test_dashboard_active_to_inactive_transitions_each_send_exactly_once(
    async_client,
    monkeypatch,
    opt_out_sender,
) -> None:
    # The fork defaults telemetry off; these opt-out paths need it active first.
    monkeypatch.setenv("CODEX_LB_TELEMETRY_ENABLED", "true")
    get_settings.cache_clear()

    first = await async_client.put("/api/settings/telemetry", json={"enabled": False})
    assert first.status_code == 200
    await asyncio.sleep(0)
    assert opt_out_sender.send_opt_out.await_count == 1

    repeated = await async_client.put("/api/settings/telemetry", json={"enabled": False})
    assert repeated.status_code == 200
    await asyncio.sleep(0)
    assert opt_out_sender.send_opt_out.await_count == 1

    enabled = await async_client.put("/api/settings/telemetry", json={"enabled": True})
    assert enabled.status_code == 200
    await asyncio.sleep(0)
    assert opt_out_sender.send_opt_out.await_count == 1

    second = await async_client.put("/api/settings/telemetry", json={"enabled": False})
    assert second.status_code == 200
    await asyncio.sleep(0)
    assert opt_out_sender.send_opt_out.await_count == 2

    call = opt_out_sender.send_opt_out.await_args_list[-1]
    assert call.args[0].instance_id
    assert call.kwargs["app_version"]
    assert call.kwargs["deployment_mode"] in {"docker", "k8s", "pip", "bare"}
    assert "/" in call.kwargs["os_arch"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env_value", "enabled", "expected_state", "expected_opt_outs"),
    [("true", False, "disabled", 1), ("false", True, "enabled", 0)],
)
async def test_persisted_dashboard_decision_wins_over_environment(
    async_client,
    monkeypatch,
    opt_out_sender,
    env_value: str,
    enabled: bool,
    expected_state: str,
    expected_opt_outs: int,
) -> None:
    monkeypatch.setenv("CODEX_LB_TELEMETRY_ENABLED", env_value)
    get_settings.cache_clear()

    # While undecided the environment decides and is reported as the source.
    before = await async_client.get("/api/settings/telemetry")
    assert before.status_code == 200
    assert before.json()["source"] == "env"
    assert before.json()["active"] is (env_value == "true")

    response = await async_client.put("/api/settings/telemetry", json={"enabled": enabled})

    assert response.status_code == 200
    assert response.json()["state"] == expected_state
    assert response.json()["source"] == "persisted"
    assert response.json()["active"] is enabled
    # The env value is still set; the saved decision must keep winning on read.
    after = await async_client.get("/api/settings/telemetry")
    assert after.status_code == 200
    assert after.json()["state"] == expected_state
    assert after.json()["source"] == "persisted"
    assert after.json()["active"] is enabled
    await asyncio.sleep(0)
    # An env-active -> dashboard-disabled transition is a dashboard-driven
    # opt-out and sends exactly one notice; enabling never does.
    assert opt_out_sender.send_opt_out.await_count == expected_opt_outs


@pytest.mark.asyncio
async def test_opt_out_background_send_does_not_block_settings_response(
    async_client,
    monkeypatch,
    opt_out_sender,
) -> None:
    # The fork defaults telemetry off; these opt-out paths need it active first.
    monkeypatch.setenv("CODEX_LB_TELEMETRY_ENABLED", "true")
    get_settings.cache_clear()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_send(*args, **kwargs) -> None:
        del args, kwargs
        started.set()
        await release.wait()

    opt_out_sender.send_opt_out.side_effect = blocked_send

    response = await async_client.put("/api/settings/telemetry", json={"enabled": False})

    assert response.status_code == 200
    await asyncio.wait_for(started.wait(), timeout=1)
    from app.modules.telemetry import api as telemetry_api

    assert telemetry_api._OPT_OUT_TASKS
    release.set()
    await asyncio.gather(*tuple(telemetry_api._OPT_OUT_TASKS))
    await asyncio.sleep(0)
    assert not telemetry_api._OPT_OUT_TASKS


@pytest.mark.asyncio
async def test_opt_out_identity_failure_is_debug_only_and_preserves_disabled_state(
    async_client,
    monkeypatch,
    opt_out_sender,
    caplog,
) -> None:
    # The fork defaults telemetry off; these opt-out paths need it active first.
    monkeypatch.setenv("CODEX_LB_TELEMETRY_ENABLED", "true")
    get_settings.cache_clear()

    async def fail_identity(_store) -> None:
        raise RuntimeError("identity decryption failed")

    monkeypatch.setattr(
        "app.modules.telemetry.api.TelemetryConsentStore.get_or_create_identity",
        fail_identity,
    )

    with caplog.at_level(logging.DEBUG, logger="app.modules.telemetry.api"):
        response = await async_client.put("/api/settings/telemetry", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["state"] == "disabled"
    persisted = await async_client.get("/api/settings/telemetry")
    assert persisted.status_code == 200
    assert persisted.json()["state"] == "disabled"
    opt_out_sender.send_opt_out.assert_not_awaited()
    assert "Unable to schedule anonymous telemetry opt-out" in caplog.messages
    assert all(record.levelno == logging.DEBUG for record in caplog.records)


@pytest.mark.asyncio
async def test_unexpected_opt_out_task_failure_is_debug_only_and_does_not_change_response(
    async_client,
    monkeypatch,
    opt_out_sender,
    caplog,
) -> None:
    # The fork defaults telemetry off; these opt-out paths need it active first.
    monkeypatch.setenv("CODEX_LB_TELEMETRY_ENABLED", "true")
    get_settings.cache_clear()
    opt_out_sender.send_opt_out.side_effect = RuntimeError("unexpected sender failure")

    with caplog.at_level(logging.DEBUG, logger="app.modules.telemetry.api"):
        response = await async_client.put("/api/settings/telemetry", json={"enabled": False})
        await asyncio.sleep(0)

    assert response.status_code == 200
    assert caplog.records
    assert all(record.levelno == logging.DEBUG for record in caplog.records)
