from __future__ import annotations

import asyncio
import json
import zlib
from functools import partial
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import aiohttp
import pytest
from fastapi.testclient import TestClient
from websockets.exceptions import ConnectionClosedError

from app.core.clients.native_egress import NativeEgressTransportError, NativeWebSocketMessage
from app.core.clients.proxy_websocket import (
    CodexUpstreamWebSocket,
    NativeUpstreamWebSocket,
    WebsocketsUpstreamWebSocket,
)
from app.core.utils.time import utcnow
from app.db.models import Account, AccountStatus
from app.dependencies import get_proxy_service_for_app
from app.modules.proxy import api as proxy_api
from app.modules.proxy import service as proxy_service
from tests.integration.test_proxy_websocket_responses import (
    _FakeUpstreamMessage,
    _SequencedUpstreamWebSocket,
    _websocket_settings,
)

pytestmark = pytest.mark.integration

_RESPONSE_ID = "resp_frameless"


def only_account() -> Account:
    # The terminal health write only runs for a real Account, so a stand-in
    # object would make the no-penalty assertions vacuous.
    return Account(
        id="only-account",
        chatgpt_account_id="only-account",
        email="only-account@example.com",
        plan_type="plus",
        access_token_encrypted=b"access",
        refresh_token_encrypted=b"refresh",
        id_token_encrypted=b"id",
        last_refresh=utcnow(),
        status=AccountStatus.ACTIVE,
    )


def _output_events(response_id: str = _RESPONSE_ID) -> list[str]:
    return [
        json.dumps({"type": "response.created", "response": {"id": response_id, "status": "in_progress"}}),
        json.dumps(
            {
                "type": "response.output_text.delta",
                "response_id": response_id,
                "delta": "partial",
                "output_index": 0,
                "content_index": 0,
            }
        ),
    ]


def _completed_events(response_id: str) -> list[_FakeUpstreamMessage]:
    return [
        _FakeUpstreamMessage(
            "text",
            text=json.dumps({"type": "response.created", "response": {"id": response_id, "status": "in_progress"}}),
        ),
        _FakeUpstreamMessage(
            "text",
            text=json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": response_id,
                        "status": "completed",
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    },
                }
            ),
        ),
    ]


class _DroppedConnection:
    """A websockets connection that vanishes without a close frame after its events."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.sent_text: list[str] = []
        self._incoming: asyncio.Queue[str | None] = asyncio.Queue()

    async def send(self, message: str | bytes) -> None:
        assert isinstance(message, str)
        self.sent_text.append(message)
        for event in self.events:
            self._incoming.put_nowait(event)
        self._incoming.put_nowait(None)

    async def recv(self) -> str:
        item = await self._incoming.get()
        if item is None:
            # The shape websockets raises when the TCP connection is lost:
            # no close frame received or sent.
            raise ConnectionClosedError(None, None)
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del code, reason


class _NativeScript:
    """A native-helper WebSocket that plays its script once the request is sent."""

    def __init__(self, script: list[NativeWebSocketMessage | BaseException]) -> None:
        self.script = script
        self.sent_text: list[str] = []
        self._incoming: asyncio.Queue[NativeWebSocketMessage | BaseException] = asyncio.Queue()

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)
        for item in self.script:
            self._incoming.put_nowait(item)

    async def send_bytes(self, data: bytes) -> None:
        raise AssertionError(f"unexpected binary frame: {data!r}")

    async def receive(self) -> NativeWebSocketMessage:
        item = await self._incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del code, reason

    def response_header(self, name: str) -> str | None:
        del name
        return None


class _RoutedScript:
    """An aiohttp client WebSocket that streams output, then ends as scripted."""

    def __init__(self, ending: BaseException | aiohttp.WSMessage, *, close_code: int | None = None) -> None:
        self.ending = (
            ending
            if isinstance(ending, aiohttp.WSMessage)
            else aiohttp.WSMessage(aiohttp.WSMsgType.ERROR, ending, None)
        )
        self.sent_text: list[str] = []
        # aiohttp stores 1006 here after an abnormal closure.
        self.close_code = close_code
        self._incoming: asyncio.Queue[aiohttp.WSMessage] = asyncio.Queue()

    async def send_str(self, text: str) -> None:
        self.sent_text.append(text)
        for event in _output_events():
            self._incoming.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, event, None))
        self._incoming.put_nowait(self.ending)

    async def send_bytes(self, data: bytes) -> None:
        raise AssertionError(f"unexpected binary frame: {data!r}")

    async def receive(self) -> aiohttp.WSMessage:
        return await self._incoming.get()

    async def close(self, *, code: int = 1000, message: bytes = b"") -> None:
        del code, message

    def exception(self) -> BaseException | None:
        return None


def _native_output() -> list[NativeWebSocketMessage | BaseException]:
    return [NativeWebSocketMessage(kind="text", text=text) for text in _output_events()]


def _install(monkeypatch, connect: AsyncMock) -> AsyncMock:
    health = AsyncMock()
    monkeypatch.setattr(proxy_api, "_websocket_firewall_denial_response", AsyncMock(return_value=None))
    monkeypatch.setattr(proxy_api, "validate_proxy_api_key_authorization", AsyncMock(return_value=None))
    monkeypatch.setattr(
        proxy_service, "get_settings_cache", lambda: SimpleNamespace(get=AsyncMock(return_value=_websocket_settings()))
    )
    monkeypatch.setattr(proxy_service.ProxyService, "_connect_proxy_websocket", connect)
    monkeypatch.setattr(proxy_service.ProxyService, "_handle_stream_error", health)
    monkeypatch.setattr(proxy_service.ProxyService, "_write_request_log", AsyncMock())
    return health


_BODY = {
    "type": "response.create",
    "model": "gpt-5.4",
    "instructions": "",
    "input": [{"role": "user", "content": "hello"}],
    "stream": True,
}


def _exchange(app_instance, path: str, frames: int) -> list[dict[str, Any]]:
    with TestClient(app_instance) as client:
        with client.websocket_connect(path) as websocket:
            websocket.send_text(json.dumps(_BODY))
            received = [json.loads(websocket.receive_text()) for _ in range(frames)]
        assert client.portal is not None
        client.portal.call(partial(get_proxy_service_for_app(app_instance).drain_persistence_tasks, timeout_seconds=5))
    return received


def _terminal_error(received: list[dict[str, Any]]) -> dict[str, Any]:
    terminal = received[-1]
    return terminal["response"]["error"] if terminal["type"] == "response.failed" else terminal["error"]


@pytest.mark.parametrize("path", ["/v1/responses", "/backend-api/codex/responses"])
def test_frameless_drop_after_output_fails_closed_without_account_penalty(app_instance, monkeypatch, path) -> None:
    connection = _DroppedConnection(_output_events())
    connect = AsyncMock(return_value=(only_account(), WebsocketsUpstreamWebSocket(cast(Any, connection))))
    health = _install(monkeypatch, connect)

    received = _exchange(app_instance, path, frames=3)

    assert _terminal_error(received)["code"] == "stream_incomplete"
    assert [e["delta"] for e in received if e["type"] == "response.output_text.delta"] == ["partial"]
    assert len(connection.sent_text) == 1
    connect.assert_awaited_once()
    health.assert_not_awaited()


@pytest.mark.parametrize(
    ("close_code", "penalized"),
    [(1006, False), (1002, True), (1011, True)],
    ids=["synthetic-abnormal-1006", "authored-protocol-error-1002", "authored-internal-error-1011"],
)
def test_only_upstream_authored_close_after_output_penalizes_account(
    app_instance, monkeypatch, close_code: int, penalized: bool
) -> None:
    events = [_FakeUpstreamMessage("text", text=text) for text in _output_events()]
    upstream = _SequencedUpstreamWebSocket(
        [], deferred_message_batches=[[*events, _FakeUpstreamMessage("close", close_code=close_code)]]
    )
    connect = AsyncMock(return_value=(only_account(), upstream))
    health = _install(monkeypatch, connect)

    received = _exchange(app_instance, "/backend-api/codex/responses", frames=3)

    assert _terminal_error(received)["code"] == "stream_incomplete"
    assert len(upstream.sent_text) == 1
    connect.assert_awaited_once()
    if penalized:
        health.assert_awaited_once()
        assert health.await_args is not None
        assert health.await_args.args[2] == "stream_incomplete"
    else:
        health.assert_not_awaited()


def test_frameless_drop_before_response_created_replays_without_account_penalty(app_instance, monkeypatch) -> None:
    dropped = _DroppedConnection([])
    replacement = _SequencedUpstreamWebSocket([], deferred_message_batches=[_completed_events("resp_replayed")])
    account = only_account()
    connect = AsyncMock(
        side_effect=[(account, WebsocketsUpstreamWebSocket(cast(Any, dropped))), (account, replacement)]
    )
    health = _install(monkeypatch, connect)

    received = _exchange(app_instance, "/backend-api/codex/responses", frames=2)

    assert [event["type"] for event in received] == ["response.created", "response.completed"]
    assert len(dropped.sent_text) == 1
    assert len(replacement.sent_text) == 1
    assert connect.await_count == 2
    health.assert_not_awaited()


def test_eventless_frameless_drop_that_cannot_replay_keeps_the_account_penalty(app_instance, monkeypatch) -> None:
    # The handshake succeeds but every stream drops before answering. Without
    # a penalty on the eventless drop the direct path would give such an
    # account no health signal at all.
    first, second = _DroppedConnection([]), _DroppedConnection([])
    account = only_account()
    connect = AsyncMock(
        side_effect=[
            (account, WebsocketsUpstreamWebSocket(cast(Any, first))),
            (account, WebsocketsUpstreamWebSocket(cast(Any, second))),
        ]
    )
    health = _install(monkeypatch, connect)

    received = _exchange(app_instance, "/backend-api/codex/responses", frames=1)

    assert _terminal_error(received)["code"] == "stream_incomplete"
    assert len(first.sent_text) == 1
    assert len(second.sent_text) == 1
    assert connect.await_count == 2
    health.assert_awaited_once()
    assert health.await_args is not None
    assert health.await_args.args[2] == "stream_incomplete"


@pytest.mark.parametrize(
    ("ending", "penalized"),
    [
        (NativeEgressTransportError("native websocket protocol failed", failure_phase="protocol"), True),
        (NativeEgressTransportError("native websocket transport failed", failure_phase="transport"), False),
        (NativeWebSocketMessage(kind="close"), True),
    ],
    ids=["protocol-error", "lost-connection", "close-frame-without-status"],
)
def test_native_helper_ending_after_output_penalizes_only_upstream_evidence(
    app_instance, monkeypatch, ending: NativeWebSocketMessage | BaseException, penalized: bool
) -> None:
    native = _NativeScript([*_native_output(), ending])
    connect = AsyncMock(return_value=(only_account(), NativeUpstreamWebSocket(cast(Any, native))))
    health = _install(monkeypatch, connect)

    received = _exchange(app_instance, "/backend-api/codex/responses", frames=3)

    assert _terminal_error(received)["code"] == "stream_incomplete"
    assert [e["delta"] for e in received if e["type"] == "response.output_text.delta"] == ["partial"]
    assert len(native.sent_text) == 1
    connect.assert_awaited_once()
    if penalized:
        health.assert_awaited_once()
        assert health.await_args is not None
        assert health.await_args.args[2] == "stream_incomplete"
    else:
        health.assert_not_awaited()


def test_native_protocol_error_before_response_created_replays_once(app_instance, monkeypatch) -> None:
    native = _NativeScript([NativeEgressTransportError("native websocket protocol failed", failure_phase="protocol")])
    replacement = _SequencedUpstreamWebSocket([], deferred_message_batches=[_completed_events("resp_replayed")])
    account = only_account()
    connect = AsyncMock(side_effect=[(account, NativeUpstreamWebSocket(cast(Any, native))), (account, replacement)])
    _install(monkeypatch, connect)

    received = _exchange(app_instance, "/backend-api/codex/responses", frames=2)

    assert [event["type"] for event in received] == ["response.created", "response.completed"]
    assert len(native.sent_text) == 1
    assert len(replacement.sent_text) == 1
    assert connect.await_count == 2


@pytest.mark.parametrize(
    ("error", "penalized"),
    [
        (
            aiohttp.WebSocketError(aiohttp.WSCloseCode.PROTOCOL_ERROR, "Received frame with non-zero reserved bits"),
            True,
        ),
        (zlib.error("invalid block type"), True),
        (aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, 1006, ""), True),
        (ConnectionResetError("connection reset by peer"), False),
        (aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None), False),
    ],
    ids=["protocol-violation", "routed-corrupt-deflate", "peer-close-1006", "lost-connection", "stored-1006"],
)
def test_routed_adapter_error_after_output_penalizes_only_a_protocol_violation(
    app_instance, monkeypatch, error: BaseException | aiohttp.WSMessage, penalized: bool
) -> None:
    routed = _RoutedScript(error, close_code=1006)
    connect = AsyncMock(return_value=(only_account(), CodexUpstreamWebSocket(routed)))
    health = _install(monkeypatch, connect)

    received = _exchange(app_instance, "/backend-api/codex/responses", frames=3)

    assert _terminal_error(received)["code"] == "stream_incomplete"
    assert [e["delta"] for e in received if e["type"] == "response.output_text.delta"] == ["partial"]
    assert len(routed.sent_text) == 1
    connect.assert_awaited_once()
    if penalized:
        health.assert_awaited_once()
        assert health.await_args is not None
        assert health.await_args.args[2] == "stream_incomplete"
    else:
        health.assert_not_awaited()
