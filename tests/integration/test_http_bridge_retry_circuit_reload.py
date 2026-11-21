"""Reloading a hard-key retry circuit must not manufacture a half-open probe.

Issue 2272: a persisted circuit row whose cooldown is absent, zero or already
elapsed was mapped onto "a cooldown that ends this instant". The admission
gate then read that value as an expired cooldown, armed the half-open lease
and suppressed every sibling request on the key for a cooldown that had
already ended. A cooldown this worker watched run must still admit exactly
one probe once it expires.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import pytest

import app.modules.proxy.service as proxy_module
from app.core.clients.proxy_websocket import UpstreamWebSocketMessage
from app.dependencies import get_proxy_service_for_app
from app.modules.proxy._service.http_bridge import retry_circuit as retry_circuit_module
from app.modules.proxy.load_balancer import AccountSelection
from tests.integration.test_http_responses_bridge import (
    _cleanup_http_bridge_sessions,  # noqa: F401
    _FakeBridgeUpstreamWebSocket,
    _get_account,
    _import_account,
    _install_bridge_settings,
)

pytestmark = pytest.mark.integration

_PATH = "/backend-api/codex/responses"
_THRESHOLD = retry_circuit_module._HTTP_BRIDGE_RETRY_CIRCUIT_FAILURE_THRESHOLD


class _HeldUpstreamWebSocket(_FakeBridgeUpstreamWebSocket):
    """Keeps every response in flight until the test releases it."""

    def __init__(self) -> None:
        super().__init__("resp_reload")
        self.release = asyncio.Event()
        self._held: list[UpstreamWebSocketMessage] = []

    async def send_text(self, text: str) -> None:
        await super().send_text(text)
        created = self._messages.get_nowait()
        completed = self._messages.get_nowait()
        self._messages.put_nowait(created)
        self._held.append(completed)

    async def receive(self) -> UpstreamWebSocketMessage:
        if self._messages.empty() and self._held:
            await self.release.wait()
            while self._held:
                self._messages.put_nowait(self._held.pop(0))
        return await super().receive()


class _OffsetClock:
    """The real clock shifted forward, so a cooldown can expire without waiting."""

    def __init__(self) -> None:
        self.offset = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.offset

    def time(self) -> float:
        return time.time() + self.offset


async def _install_upstream(async_client, monkeypatch, slug: str) -> _HeldUpstreamWebSocket:
    _install_bridge_settings(monkeypatch, enabled=True)
    account_id = await _import_account(async_client, f"acc_{slug}", f"{slug}@example.com")
    account = await _get_account(account_id)
    upstream = _HeldUpstreamWebSocket()

    async def select_account(self, *args, **kwargs):
        del self, args, kwargs
        return AccountSelection(account=account, error_message=None, error_code=None)

    async def fresh(self, target, **kwargs):
        del self, kwargs
        return target

    async def connect(*args, **kwargs):
        del args, kwargs
        return upstream

    monkeypatch.setattr(proxy_module.ProxyService, "_select_account_with_budget", select_account)
    monkeypatch.setattr(proxy_module.ProxyService, "_ensure_fresh_with_budget", fresh)
    monkeypatch.setattr(proxy_module, "connect_responses_websocket", connect)
    return upstream


def _record_admissions(monkeypatch) -> list[bool]:
    admissions: list[bool] = []
    original = proxy_module.ProxyService._http_bridge_precreated_retry_allowed

    async def recording(self, session, **kwargs):
        allowed = await original(self, session, **kwargs)
        admissions.append(allowed)
        return allowed

    monkeypatch.setattr(proxy_module.ProxyService, "_http_bridge_precreated_retry_allowed", recording)
    return admissions


async def _seed_circuit(service: Any, turn_state: str, *, cooldown_until_epoch: float) -> None:
    now = time.time()
    await service._durable_bridge.persist_retry_circuit(
        session_key_kind="turn_state_header",
        session_key_value=turn_state,
        api_key_id=None,
        consecutive_failures=_THRESHOLD,
        cooldown_until_epoch=cooldown_until_epoch,
        last_detail="clean_close",
        updated_at_epoch=now - 1.0,
        failure_threshold=_THRESHOLD,
    )
    row = await service._durable_bridge.lookup_retry_circuit(
        session_key_kind="turn_state_header",
        session_key_value=turn_state,
        api_key_id=None,
    )
    assert row is not None
    assert row.consecutive_failures == _THRESHOLD
    assert row.cooldown_until_epoch == cooldown_until_epoch


async def _wait_for(predicate, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting for the bridge"
        await asyncio.sleep(0.01)


def _post(async_client, turn_state: str, text: str):
    return async_client.post(
        _PATH,
        json={"model": "gpt-5.4", "instructions": "Return OK.", "input": text, "stream": True},
        headers={"x-codex-turn-state": turn_state},
    )


async def _two_concurrent_requests(async_client, upstream, admissions, turn_state: str):
    first = asyncio.create_task(_post(async_client, turn_state, "first"))
    # The first request is admitted and in flight upstream before the second
    # one reaches the admission gate.
    await _wait_for(lambda: admissions == [True] and len(upstream.sent_text) == 1)
    second = asyncio.create_task(_post(async_client, turn_state, "second"))
    await _wait_for(lambda: len(admissions) >= 2)
    upstream.release.set()
    return await asyncio.gather(first, second)


@pytest.mark.asyncio
@pytest.mark.parametrize("cooldown", ["zero", "elapsed"])
async def test_reloaded_ended_cooldown_admits_concurrent_requests_without_half_open(
    async_client,
    app_instance,
    monkeypatch,
    caplog,
    cooldown: str,
) -> None:
    upstream = await _install_upstream(async_client, monkeypatch, f"circuit-reload-{cooldown}")
    service = get_proxy_service_for_app(app_instance)
    turn_state = f"turn-state-circuit-reload-{cooldown}"
    await _seed_circuit(
        service,
        turn_state,
        cooldown_until_epoch=0.0 if cooldown == "zero" else time.time() - 30.0,
    )
    admissions = _record_admissions(monkeypatch)

    with caplog.at_level(logging.INFO, logger=retry_circuit_module.logger.name):
        first, second = await _two_concurrent_requests(async_client, upstream, admissions, turn_state)

    assert admissions == [True, True]
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert len(upstream.sent_text) == 2
    assert not [record for record in caplog.records if "event=half_open" in record.getMessage()]


@pytest.mark.asyncio
async def test_observed_cooldown_expiry_still_admits_exactly_one_probe(
    async_client,
    app_instance,
    monkeypatch,
    caplog,
) -> None:
    upstream = await _install_upstream(async_client, monkeypatch, "circuit-reload-real")
    service = get_proxy_service_for_app(app_instance)
    turn_state = "turn-state-circuit-reload-real"
    cooldown_seconds = 60.0
    await _seed_circuit(service, turn_state, cooldown_until_epoch=time.time() + cooldown_seconds)
    admissions = _record_admissions(monkeypatch)
    clock = _OffsetClock()
    monkeypatch.setattr(retry_circuit_module, "clock_for", lambda _owner: clock)

    cooling = await _post(async_client, turn_state, "cooling")
    assert cooling.status_code == 503, cooling.text
    assert not upstream.sent_text
    # Expire the cooldown on the circuit's own clock, wall and monotonic alike.
    clock.offset = cooldown_seconds + 1.0
    admissions.clear()

    with caplog.at_level(logging.INFO, logger=retry_circuit_module.logger.name):
        probe, sibling = await _two_concurrent_requests(async_client, upstream, admissions, turn_state)

    assert admissions == [True, False]
    assert probe.status_code == 200, probe.text
    assert sibling.status_code == 503, sibling.text
    assert json.loads(sibling.text)["error"]["code"] == "upstream_request_timeout"
    assert len(upstream.sent_text) == 1
    assert len([record for record in caplog.records if "event=half_open" in record.getMessage()]) == 1
