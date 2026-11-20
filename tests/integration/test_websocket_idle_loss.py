from __future__ import annotations

import asyncio
import json
import threading
from collections import deque

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket

import app.modules.proxy.api as proxy_api_module
import app.modules.proxy.service as proxy_module
from app.db.models import Account, AccountStatus, ApiKeyUsageReservation
from app.db.session import SessionLocal
from app.modules.api_keys.repository import ApiKeysRepository
from app.modules.api_keys.service import ApiKeyCreateData, ApiKeysService, LimitRuleInput
from app.modules.proxy._load_balancer.types import AccountLease
from app.modules.proxy.account_cache import clear_account_routing_unavailable
from tests.integration.test_proxy_websocket_responses import (
    _assert_ws_single_response_lifecycle_completed,
    _completed_first_turn_upstream_batch,
    _FakeUpstreamMessage,
    _receive_until_terminal,
    _SequencedUpstreamWebSocket,
    _websocket_response_batch,
    _websocket_settings,
    _ws_event,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(20)]

_PATH = "/backend-api/codex/responses"
_FIRST = {"role": "user", "content": [{"type": "input_text", "text": "Remember amber-moon-728."}]}
_DELTA = {"role": "user", "content": [{"type": "input_text", "text": "Repeat the code."}]}
_PARENT = "resp_retained_first"


def _create(items, *, parent=None):
    payload = {"type": "response.create", "model": "gpt-5.4", "input": items, "stream": True}
    if parent is not None:
        payload["previous_response_id"] = parent
    return payload


class _IdleSocketFixture:
    """Keep the API and database real while replacing the external transport."""

    def __init__(self, monkeypatch, upstreams, *, limited=False):
        self.upstreams = deque(upstreams)
        self.connects = []
        self.released_leases = []
        self.keys = {}
        self.account = None
        self.limited = limited
        fixture = self
        original_release = proxy_module.LoadBalancer.release_account_lease

        class SettingsCache:
            async def get(self):
                return _websocket_settings()

        async def allow_firewall(_websocket):
            return None

        async def authenticate(authorization, *, request=None):
            return fixture.keys[authorization]

        async def connect(service, headers, *, request_state, **kwargs):
            assert fixture.account is not None
            lease_id = f"retained-test-{len(fixture.connects)}"
            request_state.websocket_stream_lease = AccountLease(
                lease_id=lease_id,
                account_id=fixture.account.id,
                kind="stream",
                acquired_at=0.0,
                api_key_id=request_state.api_key.id,
            )
            fixture.connects.append(lease_id)
            return fixture.account, fixture.upstreams.popleft()

        async def release(load_balancer, lease):
            if lease is not None and lease.lease_id.startswith("retained-test-"):
                fixture.released_leases.append(lease.lease_id)
                return
            await original_release(load_balancer, lease)

        async def write_request_log(service, **kwargs):
            return None

        monkeypatch.setattr(proxy_api_module, "_websocket_firewall_denial_response", allow_firewall)
        monkeypatch.setattr(proxy_api_module, "validate_proxy_api_key_authorization", authenticate)
        monkeypatch.setattr(proxy_module, "get_settings_cache", lambda: SettingsCache())
        monkeypatch.setattr(proxy_module.ProxyService, "_connect_proxy_websocket", connect)
        monkeypatch.setattr(proxy_module.ProxyService, "_write_request_log", write_request_log)
        monkeypatch.setattr(proxy_module.LoadBalancer, "release_account_lease", release)

    async def prepare(self):
        async with SessionLocal() as session:
            account = Account(
                id="retained_socket_owner",
                chatgpt_account_id="retained_socket_owner",
                email="retained-socket@example.com",
                plan_type="plus",
                access_token_encrypted=b"access",
                refresh_token_encrypted=b"refresh",
                id_token_encrypted=b"id",
                last_refresh=proxy_module.utcnow(),
                status=AccountStatus.ACTIVE,
            )
            session.add(account)
            await session.commit()
            await session.refresh(account)
            session.expunge(account)
            self.account = account
            clear_account_routing_unavailable(account.id)
        async with SessionLocal() as session:
            service = ApiKeysService(ApiKeysRepository(session))
            for name in ("first", "second"):
                key = await service.create_key(
                    ApiKeyCreateData(
                        name=f"retained-{name}",
                        allowed_models=None,
                        limits=(
                            [LimitRuleInput(limit_type="total_tokens", limit_window="weekly", max_value=1_000_000)]
                            if self.limited
                            else []
                        ),
                    )
                )
                self.keys[f"Bearer {name}"] = await service.get_key_by_id(key.id)

    def headers(self, *, key="first", session="retained-session", conversation="retained-conversation"):
        return {"Authorization": f"Bearer {key}", "session_id": session, "thread-id": conversation}


def _complete(websocket, payload, expected):
    websocket.send_json(payload)
    events, disconnect = _receive_until_terminal(websocket)
    assert _assert_ws_single_response_lifecycle_completed(events, disconnect) == expected


@pytest.mark.parametrize("ending", ["close", "error"])
def test_idle_upstream_loss_signals_reconnect_and_preserves_full_context(app_instance, monkeypatch, ending):
    first = _SequencedUpstreamWebSocket([], deferred_message_batches=[_completed_first_turn_upstream_batch(_PARENT)])
    next_socket = _SequencedUpstreamWebSocket(
        [], deferred_message_batches=[_completed_first_turn_upstream_batch("resp_recovered")]
    )
    fixture = _IdleSocketFixture(monkeypatch, [first, next_socket])
    closed = threading.Event()
    close_codes = []
    original_close = WebSocket.close

    async def close(ws, code=1000, reason=None):
        close_codes.append(code)
        await original_close(ws, code, reason)
        closed.set()

    monkeypatch.setattr(WebSocket, "close", close)
    with TestClient(app_instance) as client:
        assert client.portal is not None
        client.portal.call(fixture.prepare)
        with client.websocket_connect("/backend-api/codex/responses", headers=fixture.headers()) as downstream:
            _complete(downstream, _create([_FIRST]), _PARENT)
            client.portal.call(first._messages.put_nowait, _FakeUpstreamMessage(kind=ending, close_code=1000))
            assert closed.wait(2), "client was left connected to a lost upstream cache"
            assert close_codes[0] == 1012
        full_context = [_FIRST, {"role": "assistant", "content": [{"type": "output_text", "text": "ACK"}]}, _DELTA]
        with client.websocket_connect("/backend-api/codex/responses", headers=fixture.headers()) as downstream:
            _complete(downstream, _create(full_context), "resp_recovered")
            sent = json.loads(next_socket.sent_text[0])
            assert sent["input"] == full_context
            assert not sent.get("previous_response_id")
    assert fixture.released_leases == ["retained-test-0", "retained-test-1"]


@pytest.mark.parametrize("pause_at", ["preparation", "admission"])
def test_idle_loss_during_request_preparation_never_dispatches_the_late_turn(app_instance, monkeypatch, pause_at):
    first = _SequencedUpstreamWebSocket([], deferred_message_batches=[_completed_first_turn_upstream_batch(_PARENT)])
    replacement = _SequencedUpstreamWebSocket([])
    fixture = _IdleSocketFixture(monkeypatch, [first, replacement], limited=True)
    preparing = threading.Event()
    closed = threading.Event()
    finished = threading.Event()
    resume = asyncio.Event()
    states = []
    reservation_ids = []
    original_prepare = proxy_module.ProxyService._prepare_websocket_response_create_request
    original_admit = proxy_module.ProxyService._acquire_request_state_response_create_admission
    original_proxy = proxy_module.ProxyService.proxy_responses_websocket
    original_close = WebSocket.close

    async def prepare(service, *args, **kwargs):
        result = await original_prepare(service, *args, **kwargs)
        states.append(result.request_state)
        assert result.request_state.api_key_reservation is not None
        reservation_ids.append(result.request_state.api_key_reservation.reservation_id)
        if len(states) == 2 and pause_at == "preparation":
            preparing.set()
            await resume.wait()
        return result

    async def admit(service, state, **kwargs):
        await original_admit(service, state, **kwargs)
        if len(states) == 2 and pause_at == "admission":
            preparing.set()
            await resume.wait()

    async def proxy(service, *args, **kwargs):
        try:
            await original_proxy(service, *args, **kwargs)
        finally:
            finished.set()

    async def assert_reservation_released():
        async with SessionLocal() as session:
            row = await session.get(ApiKeyUsageReservation, reservation_ids[1])
            assert row is not None and row.status == "released"

    async def close(ws, code=1000, reason=None):
        await original_close(ws, code, reason)
        if code == 1012:
            closed.set()

    logs = []

    async def record_request_log(service, **kwargs):
        logs.append(kwargs)

    monkeypatch.setattr(proxy_module.ProxyService, "_write_request_log", record_request_log)
    monkeypatch.setattr(proxy_module.ProxyService, "_prepare_websocket_response_create_request", prepare)
    monkeypatch.setattr(proxy_module.ProxyService, "_acquire_request_state_response_create_admission", admit)
    monkeypatch.setattr(proxy_module.ProxyService, "proxy_responses_websocket", proxy)
    monkeypatch.setattr(WebSocket, "close", close)
    with TestClient(app_instance) as client:
        assert client.portal is not None
        client.portal.call(fixture.prepare)
        with client.websocket_connect(_PATH, headers=fixture.headers()) as downstream:
            _complete(downstream, _create([_FIRST]), _PARENT)
            downstream.send_json(_create([_DELTA]))
            assert preparing.wait(2)
            client.portal.call(first._messages.put_nowait, _FakeUpstreamMessage(kind="close", close_code=1000))
            assert closed.wait(2)
            client.portal.call(resume.set)
            stopped = finished.wait(2)
            assert len(first.sent_text) + len(replacement.sent_text) == 1, (
                "late turn dispatched after the client was told to reconnect"
            )
            assert stopped, "disconnected scope retained the prepared turn"
            assert states[1].api_key_reservation is None
            assert not states[1].response_create_gate_acquired
            client.portal.call(assert_reservation_released)
    assert fixture.released_leases == ["retained-test-0"]
    # The proxy closed the downstream; the dropped turn is not a client disconnect.
    late_turn_logs = [log for log in logs if log.get("status") != "success"]
    assert [log.get("error_code") for log in late_turn_logs] == ["stream_incomplete"]


def _previous_response_miss(response_id):
    return _FakeUpstreamMessage(
        "text",
        text=json.dumps(
            {
                "type": "error",
                "status": 400,
                "error": {
                    "type": "invalid_request_error",
                    "code": "previous_response_not_found",
                    "param": "previous_response_id",
                    "message": f"Previous response with id '{response_id}' not found.",
                },
            }
        ),
    )


def test_new_socket_does_not_replay_anchored_input_without_the_prior_reply(app_instance, monkeypatch):
    # Socket A completes the first turn. Socket B shares its thread headers and
    # anchors on that response with an input that repeats A's input prefix but
    # not A's reply. Replaying it without the anchor would silently drop the
    # reply, so the miss must reach the client instead.
    first = _SequencedUpstreamWebSocket([], deferred_message_batches=[_completed_first_turn_upstream_batch(_PARENT)])
    miss = _SequencedUpstreamWebSocket([], deferred_message_batches=[[_previous_response_miss(_PARENT)]])
    replay = _SequencedUpstreamWebSocket(
        [], deferred_message_batches=[_completed_first_turn_upstream_batch("resp_replay")]
    )
    fixture = _IdleSocketFixture(monkeypatch, [first, miss, replay])
    with TestClient(app_instance) as client:
        assert client.portal is not None
        client.portal.call(fixture.prepare)
        with client.websocket_connect(_PATH, headers=fixture.headers()) as downstream:
            _complete(downstream, _create([_FIRST]), _PARENT)
        with client.websocket_connect(_PATH, headers=fixture.headers()) as downstream:
            downstream.send_json(_create([_FIRST, _DELTA], parent=_PARENT))
            events, _disconnect = _receive_until_terminal(downstream)
    assert replay.sent_text == []
    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["error"]["code"] == "previous_response_not_found"


def test_idle_upstream_loss_after_an_interrupted_response_signals_reconnect(app_instance, monkeypatch):
    # Codex treats a response that ended ``incomplete`` with reason
    # ``interrupted`` as the parent of its next turn, so losing the idle
    # upstream behind it loses that context just like after a completion.
    batch = _completed_first_turn_upstream_batch(_PARENT)
    batch[-1] = _ws_event(
        {
            "type": "response.incomplete",
            "response": {"id": _PARENT, "status": "incomplete", "incomplete_details": {"reason": "interrupted"}},
        }
    )
    first = _SequencedUpstreamWebSocket([], deferred_message_batches=[batch])
    fixture = _IdleSocketFixture(monkeypatch, [first])
    closed = threading.Event()
    close_codes = []
    original_close = WebSocket.close

    async def close(ws, code=1000, reason=None):
        close_codes.append(code)
        await original_close(ws, code, reason)
        closed.set()

    monkeypatch.setattr(WebSocket, "close", close)
    with TestClient(app_instance) as client:
        assert client.portal is not None
        client.portal.call(fixture.prepare)
        with client.websocket_connect(_PATH, headers=fixture.headers()) as downstream:
            downstream.send_json(_create([_FIRST]))
            events, disconnect = _receive_until_terminal(downstream)
            assert disconnect is None
            assert events[-1]["type"] == "response.incomplete"
            client.portal.call(first._messages.put_nowait, _FakeUpstreamMessage(kind="close", close_code=1000))
            assert closed.wait(2), "client was left connected to a lost upstream cache"
            assert close_codes[0] == 1012
    assert fixture.released_leases == ["retained-test-0"]


class _CrashingUpstreamWebSocket(_SequencedUpstreamWebSocket):
    async def receive(self):
        message = await super().receive()
        if message.kind == "crash":
            raise RuntimeError("reader bug")
        return message


def test_idle_upstream_reader_crash_signals_reconnect(app_instance, monkeypatch):
    # A crashed reader leaves the upstream unusable just like a lost one.
    first = _CrashingUpstreamWebSocket([], deferred_message_batches=[_completed_first_turn_upstream_batch(_PARENT)])
    fixture = _IdleSocketFixture(monkeypatch, [first])
    closed = threading.Event()
    close_codes = []
    original_close = WebSocket.close

    async def close(ws, code=1000, reason=None):
        close_codes.append(code)
        await original_close(ws, code, reason)
        closed.set()

    monkeypatch.setattr(WebSocket, "close", close)
    with TestClient(app_instance) as client:
        assert client.portal is not None
        client.portal.call(fixture.prepare)
        with client.websocket_connect(_PATH, headers=fixture.headers()) as downstream:
            _complete(downstream, _create([_FIRST]), _PARENT)
            client.portal.call(first._messages.put_nowait, _FakeUpstreamMessage(kind="crash"))
            assert closed.wait(2), "client was left connected to a crashed upstream reader"
            assert close_codes[0] == 1012


def test_reader_crash_leaves_a_turn_waiting_for_its_lease_with_the_sender(app_instance, monkeypatch):
    # A turn registered on the socket but still waiting for its account lease
    # was not sent on the crashed transport. The crash must neither fail it
    # nor tell the client to reconnect; the sender moves it to a new upstream
    # and dispatches it exactly once.
    first = _CrashingUpstreamWebSocket([], deferred_message_batches=[_completed_first_turn_upstream_batch(_PARENT)])
    replacement = _SequencedUpstreamWebSocket(
        [], deferred_message_batches=[_completed_first_turn_upstream_batch("resp_after_crash")]
    )
    fixture = _IdleSocketFixture(monkeypatch, [first, replacement])
    waiting = threading.Event()
    resume = asyncio.Event()
    lease_calls = []
    original_acquire = proxy_module.ProxyService._acquire_account_response_create_lease_or_overload

    async def acquire(service, *args, **kwargs):
        lease_calls.append(1)
        if len(lease_calls) == 2:
            waiting.set()
            await resume.wait()
        return await original_acquire(service, *args, **kwargs)

    close_codes = []
    original_close = WebSocket.close

    async def close(ws, code=1000, reason=None):
        close_codes.append(code)
        await original_close(ws, code, reason)

    monkeypatch.setattr(proxy_module.ProxyService, "_acquire_account_response_create_lease_or_overload", acquire)
    monkeypatch.setattr(WebSocket, "close", close)
    with TestClient(app_instance) as client:
        assert client.portal is not None
        client.portal.call(fixture.prepare)
        with client.websocket_connect(_PATH, headers=fixture.headers()) as downstream:
            _complete(downstream, _create([_FIRST]), _PARENT)
            downstream.send_json(_create([_DELTA]))
            assert waiting.wait(2)
            client.portal.call(first._messages.put_nowait, _FakeUpstreamMessage(kind="crash"))
            client.portal.call(asyncio.sleep, 0.2)
            client.portal.call(resume.set)
            events, disconnect = _receive_until_terminal(downstream)
            assert disconnect is None
            assert events[-1]["type"] == "response.completed"
            assert events[-1]["response"]["id"] == "resp_after_crash"
    assert len(first.sent_text) == 1
    assert len(replacement.sent_text) == 1
    assert 1012 not in close_codes


def test_reader_crash_during_an_active_turn_fails_it_without_a_reconnect_notice(app_instance, monkeypatch):
    # A turn already sent on the crashed transport fails as before; the
    # upstream was not idle, so the client is not told to reconnect.
    first = _CrashingUpstreamWebSocket(
        [], deferred_message_batches=[_websocket_response_batch(_PARENT, completed=False)]
    )
    fixture = _IdleSocketFixture(monkeypatch, [first])
    close_codes = []
    original_close = WebSocket.close

    async def close(ws, code=1000, reason=None):
        close_codes.append(code)
        await original_close(ws, code, reason)

    monkeypatch.setattr(WebSocket, "close", close)
    with TestClient(app_instance) as client:
        assert client.portal is not None
        client.portal.call(fixture.prepare)
        with client.websocket_connect(_PATH, headers=fixture.headers()) as downstream:
            downstream.send_json(_create([_FIRST]))
            assert downstream.receive_json()["type"] == "response.created"
            client.portal.call(first._messages.put_nowait, _FakeUpstreamMessage(kind="crash"))
            events, disconnect = _receive_until_terminal(downstream)
            assert disconnect is None
            assert events[-1]["type"] == "response.failed"
    assert 1012 not in close_codes
