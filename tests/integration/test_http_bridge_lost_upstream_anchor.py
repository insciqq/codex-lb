"""The proxy never adds an anchor to a native Codex HTTP history.

An upstream Responses WebSocket resolves a ``store=false`` ``previous_response_id``
only on the connection that produced it. The HTTP bridge used to add its own
anchor (the thread's durable anchor or the session's last response) to the
history a Codex client sends, and trimmed the history to the items after it.
When the upstream closed the socket (clean close, service restart), the
anchor was dead: upstream answered ``previous_response_not_found``, the
recovery path rebound the same denied anchor, and the client received
``502 stream_incomplete`` ("The previous response anchor was rejected upstream;
retry the request.") even though its request already carried every item.

Codex sends its complete history on every HTTP request and never anchors one,
so the bridge sends that body upstream unchanged, without an anchor, on the
thread's account and with the same ``prompt_cache_key``: prompt caching still
matches on the prefix. These tests drive the public route with synthetic
accounts and a connection-scoped fake upstream. Routing, recovery, retry and
account-health handling stay as they were; the scope guards below pin that.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from copy import deepcopy
from typing import Any, cast
from unittest.mock import AsyncMock

import anyio
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.modules.proxy.service as proxy_module
from app.core.clients.proxy_websocket import UpstreamWebSocketMessage
from app.db.models import Account
from app.db.session import SessionLocal
from app.dependencies import get_proxy_service_for_app
from app.modules.proxy._service import support as proxy_support
from app.modules.proxy._service.http_bridge import helpers as http_bridge_helpers
from app.modules.proxy._service.http_bridge import streaming as http_bridge_streaming
from app.modules.proxy.durable_bridge_repository import durable_bridge_hash
from app.modules.proxy.load_balancer import AccountSelection
from app.modules.proxy.replay_safety import anchor_free_replay_payload_is_account_neutral
from app.modules.proxy.sticky_repository import StickySessionsRepository
from tests.integration.test_http_responses_bridge import (
    _TEST_SYNC_TIMEOUT_SECONDS,
    _assert_created_text_delta_completed,
    _collect_sse_events,
    _FakeBridgeUpstreamWebSocket,
    _get_account,
    _import_account,
    _install_bridge_settings,
    _install_bridge_settings_with_limits,
    _SilentUpstreamWebSocket,
)
from tests.integration.test_http_responses_bridge import (
    _cleanup_http_bridge_sessions as cleanup_http_bridge_sessions,  # noqa: F401
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]

_ROUTE = "/backend-api/codex/responses"
_METADATA = {"turn_id": "turn_native", "create_time": 1792706040.2}


def _tool_turn_output() -> list[dict[str, Any]]:
    """A native Codex tool turn: reasoning, a commentary preamble and one call."""
    return [
        {"type": "reasoning", "id": "rs_native", "summary": [], "encrypted_content": "opaque-reasoning"},
        {
            "type": "message",
            "id": "msg_native",
            "status": "completed",
            "role": "assistant",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": "Checking the tree."}],
            "internal_chat_message_metadata_passthrough": {**_METADATA, "content_item_kinds": ["text"]},
        },
        {
            "type": "function_call",
            "id": "fc_native",
            "status": "completed",
            "call_id": "call_native",
            "name": "shell",
            "arguments": '{"cmd":"ls"}',
            "internal_chat_message_metadata_passthrough": _METADATA,
        },
    ]


def _final_answer() -> list[dict[str, Any]]:
    return [
        {
            "type": "message",
            "id": "msg_final",
            "status": "completed",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "Done."}],
        }
    ]


class _ConnectionScopedUpstream(_FakeBridgeUpstreamWebSocket):
    """Resolves ``previous_response_id`` only for responses completed on this socket."""

    def __init__(
        self,
        prefix: str,
        *,
        close_code: int,
        close_after_first_completion: bool = False,
        close_on_send: int | None = None,
        plain_answers: bool = False,
        unique_calls: bool = False,
        fleet: _UpstreamFleet | None = None,
    ) -> None:
        super().__init__(prefix)
        self.fleet = fleet
        self.completed: set[str] = set()
        self.close_code = close_code
        self.close_after_first_completion = close_after_first_completion
        self.close_on_send = close_on_send
        self.plain_answers = plain_answers
        self.unique_calls = unique_calls

    async def _put(self, event: dict[str, Any]) -> None:
        await self._messages.put(UpstreamWebSocketMessage("text", text=json.dumps(event)))

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)
        payload = json.loads(text)
        anchor = payload.get("previous_response_id")
        if self.close_on_send == len(self.sent_text) or (self.fleet is not None and self.fleet.take_drop()):
            await self._messages.put(UpstreamWebSocketMessage("close", close_code=self.close_code))
            return
        if anchor is not None and anchor not in self.completed:
            await self._put(
                {
                    "type": "error",
                    "status": 400,
                    "error": {
                        "type": "invalid_request_error",
                        "code": "previous_response_not_found",
                        "message": f"Previous response with id '{anchor}' not found.",
                        "param": "previous_response_id",
                    },
                }
            )
            return
        response_id = f"{self.response_id_prefix}_{len(self.sent_text)}"
        last_input = payload["input"][-1] if payload.get("input") else {}
        output = (
            _final_answer()
            if self.plain_answers or last_input.get("type") == "function_call_output"
            else _tool_turn_output()
        )
        if self.unique_calls:
            output = [
                {**item, "call_id": f"call_{response_id}", "id": f"fc_{response_id}"}
                if item.get("type") == "function_call"
                else item
                for item in output
            ]
        response = {"id": response_id, "object": "response", "status": "in_progress"}
        await self._put({"type": "response.created", "response": response})
        for index, item in enumerate(output):
            for event_type in ("response.output_item.added", "response.output_item.done"):
                await self._put({"type": event_type, "response_id": response_id, "output_index": index, "item": item})
        await self._put(
            {"type": "response.completed", "response": {**response, "status": "completed", "output": output}}
        )
        self.completed.add(response_id)
        if self.close_after_first_completion and len(self.completed) == 1:
            await self._messages.put(UpstreamWebSocketMessage("close", close_code=self.close_code))


class _UpstreamFleet:
    """Hands out one connection-scoped socket per upstream connect."""

    def __init__(self, **first_socket_options: Any) -> None:
        self.first_socket_options = first_socket_options
        self.sockets: list[_ConnectionScopedUpstream] = []
        self.account_ids: list[str] = []
        # Sends that any socket answers with a clean close and no event.
        self.drops = 0

    def take_drop(self) -> bool:
        if self.drops <= 0:
            return False
        self.drops -= 1
        return True

    async def connect(self, headers, access_token, account_id_header, *, base_url=None, session=None):
        del headers, access_token, base_url, session
        options = self.first_socket_options if not self.sockets else {"close_code": 1000}
        upstream = _ConnectionScopedUpstream(f"resp_conn{len(self.sockets) + 1}", fleet=self, **options)
        self.sockets.append(upstream)
        self.account_ids.append(account_id_header)
        return upstream

    def frames(self, index: int) -> list[dict[str, Any]]:
        return [json.loads(text) for text in self.sockets[index].sent_text]


def _install_fleet(monkeypatch: pytest.MonkeyPatch, fleet: _UpstreamFleet) -> None:
    async def fresh(self, target, *, force=False, timeout_seconds):
        del self, force, timeout_seconds
        return target

    # Account selection stays real so the test observes which account the
    # recovered request is routed to.
    monkeypatch.setattr(proxy_module.ProxyService, "_ensure_fresh_with_budget", fresh)
    monkeypatch.setattr(proxy_module, "connect_responses_websocket", fleet.connect)


def _body(input_items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "model": "gpt-5.1",
        "instructions": "Use the shell tool when needed.",
        "input": input_items,
        "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object", "properties": {}}}],
        "tool_choice": "auto",
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": "thread-cache-key",
    }


def _user(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


_HEADERS = {
    "session_id": "lost-anchor-session",
    "thread-id": "lost-anchor-thread",
    "originator": "Codex Desktop",
    "user-agent": "Codex Desktop/0.130.0 (Mac OS 26.1.0; arm64)",
}


def _histories() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    first_turn = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "List the tree."}]}]
    tool_resend = [
        *first_turn,
        *_tool_turn_output(),
        {
            "type": "function_call_output",
            "call_id": "call_native",
            "output": "README.md app tests",
            "internal_chat_message_metadata_passthrough": _METADATA,
        },
    ]
    return first_turn, tool_resend


async def _import_two_accounts(async_client) -> set[str]:
    chatgpt_ids = set()
    for name in ("lost_anchor_a", "lost_anchor_b"):
        account = await _get_account(await _import_account(async_client, name, f"{name}@example.com"))
        chatgpt_ids.add(str(account.chatgpt_account_id))
    return chatgpt_ids


async def _wait_until_bridge_sessions_closed(app_instance) -> None:
    service = get_proxy_service_for_app(app_instance)

    async def _poll() -> None:
        while any(not session.closed for session in service._http_bridge_sessions.values()):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=5.0)


def _terminal_event(response_text: str) -> dict[str, Any] | None:
    terminal = None
    for block in response_text.split("\n\n"):
        if "data: " not in block or "data: [DONE]" in block:
            continue
        event = json.loads(block.split("data: ", 1)[1])
        if event.get("type") in {"response.completed", "response.failed", "response.incomplete", "error"}:
            terminal = event
    return terminal


def _terminal_event_type(response_text: str) -> str | None:
    terminal = _terminal_event(response_text)
    return terminal.get("type") if terminal is not None else None


def _completed_output(response_text: str) -> list[dict[str, Any]]:
    for block in response_text.split("\n\n"):
        if "data: " not in block or "data: [DONE]" in block:
            continue
        event = json.loads(block.split("data: ", 1)[1])
        if event.get("type") == "response.completed":
            return event["response"]["output"]
    raise AssertionError("no response.completed event")


def _continuity_failures(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if "continuity_fail_closed" in record.getMessage()]


def _assert_complete_resend_on_owner(
    frame: dict[str, Any],
    *,
    client_body: dict[str, Any],
) -> None:
    assert "previous_response_id" not in frame, "a dead anchor was sent to the new upstream connection"
    assert frame["input"] == client_body["input"], "the complete client history was not sent unchanged"
    assert frame["prompt_cache_key"] == client_body["prompt_cache_key"]
    assert frame["instructions"] == client_body["instructions"]


@pytest.mark.parametrize("close_code", [1000, 1006, 1011, 1012])
@pytest.mark.asyncio
async def test_native_tool_resend_after_idle_upstream_loss_reaches_owner_complete(
    async_client, app_instance, monkeypatch, caplog, close_code
):
    """The observed production sequence, reproduced with synthetic data.

    Base behavior: ``fresh_reattach_anchor_injected`` puts the dead anchor on
    the new socket, upstream answers ``previous_response_not_found``, the local
    rebind is refused by the denied-anchor fence and the client receives
    ``502 stream_incomplete``.
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=close_code, close_after_first_completion=True)
    _install_fleet(monkeypatch, fleet)
    caplog.set_level(logging.INFO)
    first_turn, tool_resend = _histories()

    first = await async_client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
    assert first.status_code == 200
    assert _terminal_event_type(first.text) == "response.completed"
    # The upstream closes the idle socket that holds the only copy of the
    # first response.
    await _wait_until_bridge_sessions_closed(app_instance)

    resend_body = _body(deepcopy(tool_resend))
    resend = await async_client.post(_ROUTE, json=resend_body, headers=_HEADERS)

    assert resend.status_code == 200, f"tool resend failed: {resend.status_code} {resend.text[:300]}"
    assert _terminal_event_type(resend.text) == "response.completed"
    assert len(fleet.sockets) == 2
    replacement_frames = fleet.frames(1)
    assert len(replacement_frames) == 1, "the resend needed more than one upstream attempt"
    _assert_complete_resend_on_owner(replacement_frames[0], client_body=resend_body)
    # Cache affinity: the full history goes back to the account that served
    # the first turn, not to the idle second account.
    assert fleet.account_ids[1] == fleet.account_ids[0]
    assert _continuity_failures(caplog) == []


def _subagent_message() -> dict[str, Any]:
    """The item Codex appends to its history for a subagent's report."""
    return {
        "type": "agent_message",
        "id": "amsg_worker",
        "author": "/root/worker",
        "recipient": "/root",
        "content": [
            {"type": "input_text", "text": "Worker completed."},
            {"type": "encrypted_content", "encrypted_content": "opaque-agent-content"},
        ],
        "internal_chat_message_metadata_passthrough": {"turn_id": "turn_agent"},
    }


@pytest.mark.asyncio
async def test_native_history_ending_in_a_subagent_message_after_socket_loss_reaches_owner_complete(
    async_client, app_instance, monkeypatch, caplog
):
    """A subagent report at the end of the history does not bring back the dead anchor.

    Base behavior: the ``agent_message`` item failed the full-resend context
    proof, so the fresh bridge injected the durable anchor of the closed
    socket, upstream answered ``previous_response_not_found`` and the client
    received the anchor-rejected 502.
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000, close_after_first_completion=True)
    _install_fleet(monkeypatch, fleet)
    caplog.set_level(logging.INFO)
    first_turn, tool_resend = _histories()

    first = await async_client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
    assert _terminal_event_type(first.text) == "response.completed"
    await _wait_until_bridge_sessions_closed(app_instance)
    body = _body(deepcopy([*tool_resend, _subagent_message()]))
    response = await async_client.post(_ROUTE, json=body, headers=_HEADERS)

    assert response.status_code == 200, f"request failed: {response.status_code} {response.text[:300]}"
    assert _terminal_event_type(response.text) == "response.completed"
    assert len(fleet.sockets) == 2
    assert len(fleet.frames(1)) == 1
    _assert_complete_resend_on_owner(fleet.frames(1)[0], client_body=body)
    assert fleet.account_ids[1] == fleet.account_ids[0]
    assert _continuity_failures(caplog) == []


@pytest.mark.asyncio
async def test_native_tool_resend_after_zero_event_drop_is_replayed_complete(
    async_client, app_instance, monkeypatch, caplog
):
    """The socket drops before any event while the tool resend is pending.

    Base behavior: the resend carried the socket's anchor with a trimmed
    input; after the drop that anchor resolves on no other socket, and every
    attempt, the proxy's own and the client's, is refused with 502.

    Unanchored, the drop is an ordinary transport failure of the client's
    complete request: the bridge's existing replay of an unanchored request
    sends the same body on a new socket of the same account.
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000, close_on_send=2)
    _install_fleet(monkeypatch, fleet)
    caplog.set_level(logging.INFO)
    first_turn, tool_resend = _histories()

    # A native client sees an aborted stream on a transport failure; keep the
    # ASGI exception inside the transport, as a real server would.
    async with AsyncClient(
        transport=ASGITransport(app=app_instance, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        first = await client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
        assert _terminal_event_type(first.text) == "response.completed"
        resend_body = _body(deepcopy(tool_resend))
        resend = await client.post(_ROUTE, json=resend_body, headers=_HEADERS)

    assert resend.status_code == 200, f"tool resend failed: {resend.status_code} {resend.text[:300]}"
    assert _terminal_event_type(resend.text) == "response.completed"
    assert len(fleet.sockets) == 2
    dropped_frame = fleet.frames(0)[1]
    _assert_complete_resend_on_owner(dropped_frame, client_body=resend_body)
    assert fleet.frames(1) == [dropped_frame], "the replay was not the client's unchanged request"
    assert fleet.account_ids[1] == fleet.account_ids[0]
    assert not [line for line in _continuity_failures(caplog) if "previous_response_not_found" in line]


@pytest.mark.asyncio
async def test_native_tool_resend_on_live_socket_is_sent_complete(async_client, monkeypatch):
    """The socket that produced the last response is alive, and the resend still goes out complete.

    Base behavior: the bridge anchored the resend to that response and trimmed
    it to the tool output. The proxy never adds an anchor to native history:
    the frame is the client's body, on the same socket and account, with the
    same ``prompt_cache_key``, so upstream prompt caching matches its prefix.
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000)
    _install_fleet(monkeypatch, fleet)
    first_turn, tool_resend = _histories()

    first = await async_client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
    assert _terminal_event_type(first.text) == "response.completed"
    resend_body = _body(deepcopy(tool_resend))
    resend = await async_client.post(_ROUTE, json=resend_body, headers=_HEADERS)

    assert resend.status_code == 200
    assert _terminal_event_type(resend.text) == "response.completed"
    assert len(fleet.sockets) == 1
    _assert_complete_resend_on_owner(fleet.frames(0)[1], client_body=resend_body)


@pytest.mark.asyncio
async def test_native_turn_after_lost_socket_stays_complete_on_the_same_socket(async_client, app_instance, monkeypatch):
    """After the recovered resend, the thread's next turn is complete as well.

    The resend runs on the thread's own bridge, so the following turn reuses
    that socket and account and carries the client's whole history.
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1012, close_after_first_completion=True)
    _install_fleet(monkeypatch, fleet)
    first_turn, tool_resend = _histories()

    first = await async_client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
    assert _terminal_event_type(first.text) == "response.completed"
    await _wait_until_bridge_sessions_closed(app_instance)
    resend = await async_client.post(_ROUTE, json=_body(deepcopy(tool_resend)), headers=_HEADERS)
    assert _terminal_event_type(resend.text) == "response.completed"

    next_turn = [*tool_resend, *_final_answer(), _user("Now the tests.")]
    follow_up_body = _body(deepcopy(next_turn))
    follow_up = await async_client.post(_ROUTE, json=follow_up_body, headers=_HEADERS)

    assert follow_up.status_code == 200
    assert _terminal_event_type(follow_up.text) == "response.completed"
    assert len(fleet.sockets) == 2
    assert len(fleet.frames(1)) == 2
    _assert_complete_resend_on_owner(fleet.frames(1)[-1], client_body=follow_up_body)
    assert fleet.account_ids == [fleet.account_ids[0]] * 2


@pytest.mark.asyncio
async def test_native_tool_resend_after_reconnect_is_not_anchored_to_the_old_socket(
    async_client, app_instance, monkeypatch
):
    """A live bridge that replaced its socket must not send the previous socket's anchor.

    Base behavior: the session-level anchor survives a same-account reconnect,
    the new socket answers ``previous_response_not_found`` and the client
    receives the anchor-rejected 502.
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000)
    _install_fleet(monkeypatch, fleet)
    first_turn, tool_resend = _histories()

    first = await async_client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
    assert _terminal_event_type(first.text) == "response.completed"

    service = get_proxy_service_for_app(app_instance)
    session = next(iter(service._http_bridge_sessions.values()))
    replacement = _ConnectionScopedUpstream("resp_conn2", close_code=1000)
    fleet.sockets.append(replacement)
    fleet.account_ids.append(fleet.account_ids[0])
    reader = session.upstream_reader
    assert reader is not None
    reader.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await reader
    # The same transport swap a same-account reconnect performs.
    session.replace_connection(session.account, dict(session.headers), replacement, session.access_token_expires_at)
    session.closed = False
    session.upstream_control = proxy_module._WebSocketUpstreamControl()
    session.upstream_reader = service._spawn_http_bridge_upstream_reader(session)

    resend_body = _body(deepcopy(tool_resend))
    resend = await async_client.post(_ROUTE, json=resend_body, headers=_HEADERS)

    assert resend.status_code == 200, f"tool resend failed: {resend.status_code} {resend.text[:300]}"
    assert _terminal_event_type(resend.text) == "response.completed"
    assert len(fleet.sockets) == 2, "the reconnected bridge was not reused"
    replacement_frames = fleet.frames(1)
    assert len(replacement_frames) == 1
    _assert_complete_resend_on_owner(replacement_frames[0], client_body=resend_body)


@pytest.mark.asyncio
async def test_non_native_client_keeps_the_durable_anchor_after_socket_loss(async_client, app_instance, monkeypatch):
    """Scope guard: only native Codex history is treated as complete.

    Other clients may rely on the proxy's durable anchor for context they do
    not resend, so the fresh bridge still injects it for them.
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000, close_after_first_completion=True)
    _install_fleet(monkeypatch, fleet)
    first_turn, tool_resend = _histories()
    headers = {"session_id": "non-native-session", "thread-id": "non-native-thread"}

    first = await async_client.post(_ROUTE, json=_body(first_turn), headers=headers)
    assert _terminal_event_type(first.text) == "response.completed"
    await _wait_until_bridge_sessions_closed(app_instance)
    resend = await async_client.post(_ROUTE, json=_body(deepcopy(tool_resend)), headers=headers)

    assert len(fleet.sockets) >= 2
    assert fleet.frames(1)[0].get("previous_response_id") == "resp_conn1_1"
    # Base behavior for such clients: the dead anchor is refused, not dropped.
    assert resend.status_code == 502
    assert all(frame.get("previous_response_id") == "resp_conn1_1" for frame in fleet.frames(1))


@pytest.mark.asyncio
async def test_native_client_supplied_anchor_is_preserved(async_client, app_instance, monkeypatch):
    """Scope guard: a previous_response_id the client sent is never dropped by this rule."""
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000, close_after_first_completion=True)
    _install_fleet(monkeypatch, fleet)
    first_turn, tool_resend = _histories()

    first = await async_client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
    assert _terminal_event_type(first.text) == "response.completed"
    await _wait_until_bridge_sessions_closed(app_instance)
    anchored_body = {**_body(deepcopy(tool_resend)), "previous_response_id": "resp_conn1_1"}
    anchored = await async_client.post(_ROUTE, json=anchored_body, headers=_HEADERS)

    assert len(fleet.sockets) >= 2
    assert all(frame.get("previous_response_id") == "resp_conn1_1" for frame in fleet.frames(1))
    # The unresolvable anchor fails the request; it is never dropped silently.
    assert anchored.status_code != 200 or _terminal_event_type(anchored.text) != "response.completed"


@pytest.mark.asyncio
async def test_sdk_client_with_codex_originator_keeps_the_durable_anchor(async_client, app_instance, monkeypatch):
    """The bridge uses the route's identity: an explicit OpenAI SDK marker is never native Codex."""
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000, close_after_first_completion=True)
    _install_fleet(monkeypatch, fleet)
    first_turn, tool_resend = _histories()
    headers = {
        "session_id": "sdk-session",
        "thread-id": "sdk-thread",
        "originator": "Codex Desktop",
        "user-agent": "OpenAI/Python 2.24.0",
        "x-stainless-lang": "python",
    }

    first = await async_client.post(_ROUTE, json=_body(first_turn), headers=headers)
    assert _terminal_event_type(first.text) == "response.completed"
    await _wait_until_bridge_sessions_closed(app_instance)
    await async_client.post(_ROUTE, json=_body(deepcopy(tool_resend)), headers=headers)

    assert len(fleet.sockets) >= 2
    assert fleet.frames(1)[0].get("previous_response_id") == "resp_conn1_1"


@pytest.mark.asyncio
async def test_native_short_history_after_idle_loss_is_sent_complete(async_client, app_instance, monkeypatch):
    """A one-message native history is complete too; no size threshold decides it."""
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000, close_after_first_completion=True)
    _install_fleet(monkeypatch, fleet)
    first_turn, _tool_resend = _histories()

    first = await async_client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
    assert _terminal_event_type(first.text) == "response.completed"
    await _wait_until_bridge_sessions_closed(app_instance)
    resend_body = _body(deepcopy(first_turn))
    resend = await async_client.post(_ROUTE, json=resend_body, headers=_HEADERS)

    assert resend.status_code == 200, f"short resend failed: {resend.status_code} {resend.text[:300]}"
    assert len(fleet.sockets) == 2
    _assert_complete_resend_on_owner(fleet.frames(1)[0], client_body=resend_body)


@pytest.mark.asyncio
async def test_native_retry_after_zero_event_drop_is_not_fenced_by_the_recovery_journal(
    async_client, app_instance, monkeypatch
):
    """The client's identical complete retry after a zero-event drop is served.

    Before: the anchored attempt left a replay-safe recovery checkpoint in the
    UNKNOWN state, and every identical retry was refused with
    ``502 bridge_continuity_persistence_failed`` before a socket opened. An
    unanchored native request records no such checkpoint: the dropped attempt
    and the client's retry both reach the owner with the unchanged history.
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000, close_on_send=2, plain_answers=True)
    _install_fleet(monkeypatch, fleet)
    first_turn, _tool_resend = _histories()

    async with AsyncClient(
        transport=ASGITransport(app=app_instance, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        first = await client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
        assert _terminal_event_type(first.text) == "response.completed"
        history = [*first_turn, *_completed_output(first.text), _user("Next?")]
        dropped = await client.post(_ROUTE, json=_body(deepcopy(history)), headers=_HEADERS)
        retry_body = _body(deepcopy(history))
        retry = await client.post(_ROUTE, json=retry_body, headers=_HEADERS)

    # The dropped attempt is an unanchored request: the bridge's existing
    # transport replay sends the same body on a new socket of the owner.
    assert dropped.status_code == 200, f"dropped attempt: {dropped.status_code} {dropped.text[:300]}"
    assert _terminal_event_type(dropped.text) == "response.completed"
    assert retry.status_code == 200, f"client retry failed: {retry.status_code} {retry.text[:300]}"
    assert _terminal_event_type(retry.text) == "response.completed"
    assert len(fleet.sockets) == 2
    assert [len(fleet.frames(0)), len(fleet.frames(1))] == [2, 2]
    for frame in [fleet.frames(0)[1], *fleet.frames(1)]:
        _assert_complete_resend_on_owner(frame, client_body=retry_body)
    assert fleet.account_ids[1] == fleet.account_ids[0]


@pytest.mark.asyncio
async def test_native_history_is_sent_complete_after_the_socket_changes_before_dispatch(
    async_client, app_instance, monkeypatch
):
    """The socket can change between request planning and the first send.

    Base behavior: the trimmed frame went out on the new socket with the old
    socket's anchor and the client received the anchor-rejected 502. With no
    anchor, the new socket serves the client's complete body.
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000)
    _install_fleet(monkeypatch, fleet)
    first_turn, tool_resend = _histories()
    first = await async_client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
    assert _terminal_event_type(first.text) == "response.completed"

    service = get_proxy_service_for_app(app_instance)
    stream = service._stream_http_bridge_session_events

    async def reconnect_before_submit(session, **kwargs):
        if session.upstream is fleet.sockets[0]:
            await service._reconnect_http_bridge_session(
                session,
                request_state=kwargs["request_state"],
                restart_reader=True,
                require_same_account=True,
            )
        async for event in stream(session, **kwargs):
            yield event

    monkeypatch.setattr(service, "_stream_http_bridge_session_events", reconnect_before_submit)
    resend_body = _body(deepcopy(tool_resend))
    resend = await async_client.post(_ROUTE, json=resend_body, headers=_HEADERS)

    assert resend.status_code == 200, f"resend failed: {resend.status_code} {resend.text[:300]}"
    assert _terminal_event_type(resend.text) == "response.completed"
    assert len(fleet.sockets) == 2
    assert len(fleet.frames(1)) == 1
    _assert_complete_resend_on_owner(fleet.frames(1)[0], client_body=resend_body)
    assert fleet.account_ids[1] == fleet.account_ids[0]


@pytest.mark.asyncio
async def test_native_history_after_a_crash_restart_stays_on_the_healthy_owner(async_client, app_instance, monkeypatch):
    """A durable row from a previous process only means the socket is gone.

    Base behavior: the previous-epoch owner counts as dead, the request
    switches to the account-neutral replay with the healthy owner excluded,
    and the thread moves to the other account.
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000, plain_answers=True)
    _install_fleet(monkeypatch, fleet)
    first_turn, _tool_resend = _histories()
    first = await async_client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
    assert _terminal_event_type(first.text) == "response.completed"
    # Crash: in-memory sessions vanish without releasing the durable row, and
    # the restarted process has a new epoch.
    service = get_proxy_service_for_app(app_instance)
    for key, session in list(service._http_bridge_sessions.items()):
        session.closed = True
        if session.upstream_reader is not None:
            session.upstream_reader.cancel()
        service._http_bridge_sessions.pop(key, None)
    monkeypatch.setattr(http_bridge_streaming, "http_bridge_owner_process_epoch", lambda: "restarted-process")

    history = [*first_turn, *_completed_output(first.text), _user("Next?")]
    body = _body(deepcopy(history))
    response = await async_client.post(_ROUTE, json=body, headers=_HEADERS)

    assert response.status_code == 200, f"request failed: {response.status_code} {response.text[:300]}"
    assert len(fleet.sockets) == 2
    assert fleet.account_ids[1] == fleet.account_ids[0], "a restart moved the thread off its healthy owner"
    _assert_complete_resend_on_owner(fleet.frames(1)[0], client_body=body)


@pytest.mark.parametrize(
    ("first_shape", "second_shape"),
    [("tool_call", "tool_call"), ("tool_call", "final_answer"), ("final_answer", "final_answer")],
)
@pytest.mark.asyncio
async def test_native_history_from_an_earlier_response_is_not_anchored_to_a_later_one(
    async_client, monkeypatch, first_shape, second_shape
):
    """A history that continues from an earlier response is never anchored to a later one.

    The client asked twice (duplicate tool calls, a tool call and an answer,
    or duplicate answers) and continues from the first response, but the
    session's last response is the second one. Base behavior: the request was
    anchored to the second response, whose input prefix it matches, so
    upstream continued from a response the client never kept (and added a
    synthetic output for a call the client never saw).
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000, unique_calls=True, plain_answers=first_shape == "final_answer")
    _install_fleet(monkeypatch, fleet)
    first_turn, _tool_resend = _histories()

    first = await async_client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
    fleet.sockets[0].plain_answers = second_shape == "final_answer"
    again = await async_client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
    assert _terminal_event_type(again.text) == "response.completed"
    first_output = _completed_output(first.text)
    if first_shape == "tool_call":
        call = next(item for item in first_output if item["type"] == "function_call")
        continuation = {"type": "function_call_output", "call_id": call["call_id"], "output": "README.md"}
    else:
        continuation = _user("And then?")
    history = [*first_turn, *first_output, continuation]
    body = _body(deepcopy(history))
    response = await async_client.post(_ROUTE, json=body, headers=_HEADERS)

    assert response.status_code == 200
    assert _terminal_event_type(response.text) == "response.completed"
    assert len(fleet.sockets) == 1
    assert len(fleet.frames(0)) == 3
    _assert_complete_resend_on_owner(fleet.frames(0)[2], client_body=body)


@pytest.mark.asyncio
async def test_native_history_replacing_a_retired_gate_is_sent_complete(async_client, app_instance, monkeypatch):
    """A stuck response-create gate retires the session; its replacement gets the complete body.

    Base behavior: the history was anchored to the retired session's last
    response and trimmed, and the replacement session sent that prepared
    frame on its new socket, where the anchor cannot resolve.
    """
    _install_bridge_settings_with_limits(monkeypatch, enabled=True, admission_wait_timeout_seconds=0.001)
    # Native Codex HTTP stays HTTP under auto policy; opt into the bridge.
    dashboard_settings = await proxy_module.get_settings_cache().get()
    dashboard_settings.upstream_stream_transport = "websocket"
    monkeypatch.setattr(http_bridge_helpers, "HTTP_BRIDGE_STUCK_GATE_RETIRE_AFTER_SECONDS", 0.01)
    account_id = await _import_account(async_client, "lost_anchor_gate", "lost_anchor_gate@example.com")
    account = await _get_account(account_id)
    service = get_proxy_service_for_app(app_instance)
    replacement_upstream = _FakeBridgeUpstreamWebSocket()
    monkeypatch.setattr(
        service,
        "_select_account_with_budget",
        AsyncMock(return_value=AccountSelection(account=account, error_message=None, error_code=None)),
    )
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(return_value=account))

    async def connect(headers, access_token, account_id_header, *, base_url=None, session=None):
        del headers, access_token, account_id_header, base_url, session
        return replacement_upstream

    monkeypatch.setattr(proxy_module, "connect_responses_websocket", connect)

    request_headers = {
        "session_id": "lost-anchor-retired-gate",
        "x-codex-turn-state": "http_turn_lost_anchor_retired_gate",
        "user-agent": "codex_cli_rs/0.145.0",
    }
    payload = proxy_module.ResponsesRequest(
        model="gpt-5.6-sol",
        instructions="Return exactly OK.",
        input=[
            _user("old"),
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]},
            _user("next"),
        ],
    )
    affinity = proxy_module._sticky_key_for_responses_request(
        payload,
        request_headers,
        codex_session_affinity=True,
        openai_cache_affinity=True,
        openai_cache_affinity_max_age_seconds=300,
        sticky_threads_enabled=False,
        api_key=None,
    )
    key = proxy_module._HTTPBridgeSessionKey("session_header", "lost-anchor-retired-gate", None)
    stale_upstream = _SilentUpstreamWebSocket()
    gate = asyncio.Semaphore(1)
    await gate.acquire()
    stale_request = proxy_module._WebSocketRequestState(
        request_id="req-lost-anchor-stale-gate",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="high",
        api_key_reservation=None,
        started_at=time.monotonic() - 1.0,
        transport="http",
        response_create_gate=gate,
        response_create_gate_acquired=True,
        awaiting_response_created=True,
        event_queue=asyncio.Queue(),
        request_text='{"type":"response.create","model":"gpt-5.6-sol"}',
    )
    stale_session = proxy_module._HTTPBridgeSession(
        key=key,
        headers=request_headers,
        affinity=affinity,
        request_model="gpt-5.6-sol",
        account=account,
        upstream=cast(proxy_module.UpstreamWebSocket, stale_upstream),
        upstream_control=proxy_module._WebSocketUpstreamControl(),
        pending_requests=deque([stale_request]),
        pending_lock=anyio.Lock(),
        response_create_gate=gate,
        queued_request_count=1,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
        codex_session=True,
    )
    # The retired session completed the first item on its own socket.
    stale_session.last_completed_response_id = "resp_old"
    stale_session.last_completed_response_account_id = account.id
    stale_session.last_completed_input_count = 1
    assert isinstance(payload.input, list)
    stale_session.last_completed_input_prefix_fingerprint = proxy_module._fingerprint_input_items(payload.input[:1])
    stale_session.downstream_turn_state_aliases.add("http_turn_lost_anchor_retired_gate")
    service._http_bridge_sessions[key] = stale_session
    service._http_bridge_turn_state_index[
        proxy_module._http_bridge_turn_state_alias_key("http_turn_lost_anchor_retired_gate", None)
    ] = key

    try:
        response = await asyncio.wait_for(
            async_client.post(_ROUTE, json=payload.to_payload(), headers=request_headers),
            timeout=_TEST_SYNC_TIMEOUT_SECONDS,
        )
    finally:
        if gate.locked():
            gate.release()

    assert response.status_code == 200
    assert stale_session.closed is True
    assert len(replacement_upstream.sent_text) == 1
    frame = json.loads(replacement_upstream.sent_text[0])
    assert "previous_response_id" not in frame
    assert frame["input"] == payload.to_payload()["input"]


@pytest.mark.asyncio
async def test_native_verified_full_resend_ignores_stale_broad_owner_on_durable_account(
    async_client, app_instance, monkeypatch
):
    """Scope guard: routing of a native full resend is unchanged.

    A verified full resend on a fresh bridge ignores a stale raw session-header
    owner and dispatches to the healthy durable owner. Not adding the anchor
    must not skip that routing decision (it returned 503
    ``continuity_owner_conflict`` when it did).
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    owner_account = await _get_account(
        await _import_account(async_client, "lost_anchor_durable_owner", "lost_anchor_durable_owner@example.com")
    )
    stale_account = await _get_account(
        await _import_account(async_client, "lost_anchor_stale_owner", "lost_anchor_stale_owner@example.com")
    )
    service = get_proxy_service_for_app(app_instance)
    session_id = "lost-anchor-durable-full-resend"
    historical_input: list[Any] = [_user("first question")]
    claimed = await service._durable_bridge.claim_live_session(
        session_key_kind="session_header",
        session_key_value=session_id,
        api_key_id=None,
        instance_id="instance-a",
        owner_process_epoch="test-process",
        lease_ttl_seconds=60.0,
        account_id=owner_account.id,
        model="gpt-5.1",
        service_tier=None,
        latest_turn_state="http_turn_lost_anchor_full_resend",
        latest_response_id="resp_lost_anchor_previous",
        allow_takeover=True,
    )
    renewed = await service._durable_bridge.renew_live_session(
        session_id=claimed.session_id,
        api_key_id=None,
        instance_id="instance-a",
        owner_epoch=claimed.owner_epoch,
        lease_ttl_seconds=60.0,
        latest_turn_state="http_turn_lost_anchor_full_resend",
        latest_response_id="resp_lost_anchor_previous",
        latest_input_item_count=len(historical_input),
        latest_input_full_fingerprint=proxy_module._fingerprint_input_items(historical_input),
    )
    assert renewed is not None
    released = await service._durable_bridge.release_live_session(
        session_id=claimed.session_id,
        instance_id="instance-a",
        owner_epoch=claimed.owner_epoch,
        draining=False,
    )
    assert released is not None
    async with SessionLocal() as session:
        await StickySessionsRepository(session).upsert(
            session_id,
            stale_account.id,
            kind=proxy_module.StickySessionKind.CODEX_SESSION,
        )

    upstream = _FakeBridgeUpstreamWebSocket("resp_lost_anchor_full_resend")
    connect_calls: list[str] = []

    async def fresh(self, target, *, force=False, timeout_seconds):
        del self, force, timeout_seconds
        return target

    async def connect(headers, access_token, account_id_header, *, base_url=None, session=None):
        del headers, access_token, base_url, session
        connect_calls.append(account_id_header)
        return upstream

    monkeypatch.setattr(proxy_module.ProxyService, "_ensure_fresh_with_budget", fresh)
    monkeypatch.setattr(proxy_module, "connect_responses_websocket", connect)

    full_resend = [
        *historical_input,
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "first answer"}],
        },
        _user("second question"),
    ]
    events = await _collect_sse_events(
        async_client,
        _ROUTE,
        json_body={"model": "gpt-5.1", "instructions": "Return exactly OK.", "input": full_resend, "stream": True},
        headers={"session_id": session_id, "originator": "Codex Desktop"},
    )

    _assert_created_text_delta_completed(events)
    assert connect_calls == [owner_account.chatgpt_account_id]
    frame = json.loads(upstream.sent_text[0])
    assert "previous_response_id" not in frame
    assert frame["input"] == full_resend
    bridge_session = service._http_bridge_sessions[
        proxy_module._HTTPBridgeSessionKey("session_header", session_id, None)
    ]
    assert bridge_session.account.id == owner_account.id


@pytest.mark.asyncio
async def test_native_history_is_retried_after_an_accepted_capacity_error(async_client, monkeypatch):
    """Scope guard: an explicit capacity error after ``response.created`` keeps its retry."""
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000, plain_answers=True)
    _install_fleet(monkeypatch, fleet)
    first_turn, _tool_resend = _histories()
    first = await async_client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
    assert _terminal_event_type(first.text) == "response.completed"

    overloaded = fleet.sockets[0]

    async def accepted_then_overloaded(text: str) -> None:
        # Upstream accepts the request, then fails it before any output.
        overloaded.sent_text.append(text)
        response = {"id": f"resp_overloaded_{len(overloaded.sent_text)}", "object": "response", "status": "in_progress"}
        await overloaded._put({"type": "response.created", "response": response})
        await overloaded._put({"type": "response.in_progress", "response": response})
        await overloaded._put(
            {
                "type": "error",
                "error": {
                    "type": "service_unavailable_error",
                    "code": "server_is_overloaded",
                    "message": "Our servers are currently overloaded. Please try again later.",
                },
            }
        )

    monkeypatch.setattr(overloaded, "send_text", accepted_then_overloaded)
    output = [{key: value for key, value in item.items() if key != "id"} for item in _completed_output(first.text)]
    body = _body([*first_turn, *output, _user("next?")])
    response = await async_client.post(_ROUTE, json=body, headers=_HEADERS)

    assert response.status_code == 200, f"request failed: {response.status_code} {response.text[:300]}"
    assert _terminal_event_type(response.text) == "response.completed"
    # Base's retry: a new socket of the same account serves the complete body.
    assert len(fleet.sockets) == 2
    assert len(fleet.frames(1)) == 1
    _assert_complete_resend_on_owner(fleet.frames(1)[0], client_body=body)
    assert fleet.account_ids[1] == fleet.account_ids[0]


_REAUTH_ERRORS = {
    "token_invalidated": "Your session has ended. Please log in again.",
    "invalid_api_key": "Your session has ended, please log in again.",
}


@pytest.mark.parametrize("error_code", sorted(_REAUTH_ERRORS))
@pytest.mark.parametrize("native", [True, False], ids=["native", "non_native"])
@pytest.mark.asyncio
async def test_ended_session_marks_the_account_for_reauth_once(
    async_client, app_instance, monkeypatch, native, error_code
):
    """Scope guard: an ended upstream session keeps base's permanent-failure handling.

    The follow-up is bound to the account that served the thread. The account
    is marked for re-authentication exactly once, the request is not replayed,
    and the client receives the upstream 401.
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000)
    _install_fleet(monkeypatch, fleet)
    headers = _HEADERS if native else {"session_id": "reauth-session", "thread-id": "reauth-thread"}
    first_turn, tool_resend = _histories()
    first = await async_client.post(_ROUTE, json=_body(first_turn), headers=headers)
    assert _terminal_event_type(first.text) == "response.completed"
    service = get_proxy_service_for_app(app_instance)
    session = next(iter(service._http_bridge_sessions.values()))
    marks: list[str | None] = []
    mark_permanent_failure = service._load_balancer.mark_permanent_failure

    async def record_mark(account, failure_code):
        marks.append(failure_code)
        await mark_permanent_failure(account, failure_code)

    monkeypatch.setattr(service._load_balancer, "mark_permanent_failure", record_mark)

    async def expired(text: str) -> None:
        fleet.sockets[0].sent_text.append(text)
        await fleet.sockets[0]._put(
            {
                "type": "error",
                "status": 401,
                "error": {"type": "authentication_error", "code": error_code, "message": _REAUTH_ERRORS[error_code]},
            }
        )

    monkeypatch.setattr(fleet.sockets[0], "send_text", expired)
    response = await async_client.post(_ROUTE, json=_body(deepcopy(tool_resend)), headers=headers)

    assert response.status_code == 200
    assert _terminal_event(response.text) == {
        "type": "error",
        "status": 401,
        "error": {"type": "authentication_error", "code": error_code, "message": _REAUTH_ERRORS[error_code]},
    }
    assert marks == ["account_session_expired"]
    assert (await _get_account(session.account.id)).status.value == "reauth_required"
    # Not replayed: the second frame was the only attempt.
    assert len(fleet.sockets) == 1
    assert len(fleet.sockets[0].sent_text) == 2


def _id_free(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in item.items() if key != "id"} for item in items]


@pytest.mark.asyncio
async def test_native_id_free_history_after_zero_event_drop_stays_on_the_owner(async_client, app_instance, monkeypatch):
    """A transport-only replay of native history keeps the thread's owner.

    An id-free history looks account-neutral, and without a turn-state header
    the replay after a zero-event clean close treated it as a fresh request:
    the owner was excluded and the same prompt cache key went to the other
    account. Base retried the anchored request on the owner.
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000, close_on_send=2, plain_answers=True)
    _install_fleet(monkeypatch, fleet)
    first_turn, _tool_resend = _histories()

    async with AsyncClient(
        transport=ASGITransport(app=app_instance, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        first = await client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
        assert _terminal_event_type(first.text) == "response.completed"
        history = [*first_turn, *_id_free(_completed_output(first.text)), _user("Next?")]
        dropped = await client.post(_ROUTE, json=_body(deepcopy(history)), headers=_HEADERS)

    assert dropped.status_code == 200, f"dropped attempt: {dropped.status_code} {dropped.text[:300]}"
    assert _terminal_event_type(dropped.text) == "response.completed"
    assert fleet.account_ids == [fleet.account_ids[0]] * 2
    assert fleet.frames(1)[0]["prompt_cache_key"] == "thread-cache-key"


@pytest.mark.asyncio
async def test_native_history_is_not_replayed_over_raw_http_to_another_account(async_client, app_instance, monkeypatch):
    """A failed bridge connect for an owner-bound native history is not replayed over raw HTTP.

    The raw HTTP path cannot require the thread's durable owner: with a stale
    sticky row it sent the owner's encrypted reasoning to the other account.
    Base propagated the connect failure, because the bridge had prepared an
    anchor for that history, and made no raw request.
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000)
    _install_fleet(monkeypatch, fleet)
    # A raw fallback would arm the process-wide websocket failure marker.
    monkeypatch.setattr(proxy_support, "_upstream_ws_transport_failure_at", None)
    first_turn, tool_resend = _histories()
    first = await async_client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
    assert _terminal_event_type(first.text) == "response.completed"
    service = get_proxy_service_for_app(app_instance)
    session = next(iter(service._http_bridge_sessions.values()))
    await fleet.sockets[0]._messages.put(UpstreamWebSocketMessage("close", close_code=1000))
    await _wait_until_bridge_sessions_closed(app_instance)
    async with SessionLocal() as db:
        other = await db.scalar(select(Account.id).where(Account.id != session.account.id))
        assert other is not None
        await StickySessionsRepository(db).upsert(session.affinity.selection_key, other, kind=session.affinity.kind)

    async def fail_upgrade(*args: Any, **kwargs: Any) -> Any:
        raise proxy_module.ProxyResponseError(
            502,
            proxy_module.openai_error("upstream_unavailable", "upgrade failed"),
            failure_phase="connect",
            failure_detail="upstream_websocket_transport_unavailable",
        )

    raw_accounts: list[str] = []

    async def raw(payload, headers, access_token, account_id, **kwargs):
        raw_accounts.append(account_id)
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(proxy_module, "connect_responses_websocket", fail_upgrade)
    monkeypatch.setattr(proxy_module, "core_stream_responses", raw)
    async with AsyncClient(
        transport=ASGITransport(app=app_instance, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        response = await client.post(_ROUTE, json=_body(deepcopy(tool_resend)), headers=_HEADERS)

    # As on base: the connect failure reaches the client and no raw request is made.
    assert raw_accounts == []
    assert _terminal_event_type(response.text) != "response.completed"
    assert len(fleet.sockets) == 1


@pytest.mark.asyncio
async def test_native_retries_are_not_fenced_by_a_legacy_recovery_checkpoint(async_client, app_instance, monkeypatch):
    """An UNKNOWN recovery checkpoint written for an anchored attempt does not fence native history.

    Before the change the bridge anchored native history and journaled its
    unanchored body. A checkpoint left UNKNOWN that way never settles once the
    session is gone, and it refused every identical retry with 502 before any
    socket opened. Native history is complete and its tools run on the
    client, so the checkpoint protects nothing for it.
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000, plain_answers=True)
    _install_fleet(monkeypatch, fleet)
    first_turn, _tool_resend = _histories()
    first = await async_client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
    assert _terminal_event_type(first.text) == "response.completed"
    history = [*first_turn, *_completed_output(first.text), _user("Next?")]
    service = get_proxy_service_for_app(app_instance)
    session = next(iter(service._http_bridge_sessions.values()))
    payload = proxy_module.ResponsesRequest.model_validate(_body(history))
    _state, wire = service._prepare_http_bridge_request(payload, _HEADERS, api_key=None, api_key_reservation=None)
    await service._durable_bridge.record_recovery_attempt(
        session_id=session.durable_session_id,
        api_key_id=None,
        instance_id="instance-a",
        owner_epoch=session.durable_owner_epoch,
        request_fingerprint=durable_bridge_hash(wire),
        request_id="legacy_anchored_attempt",
        account_id=session.account.id,
        model=payload.model,
        replay_safe=True,
    )
    await fleet.sockets[0]._messages.put(UpstreamWebSocketMessage("close", close_code=1000))
    await _wait_until_bridge_sessions_closed(app_instance)

    for attempt in range(3):
        retry = await async_client.post(_ROUTE, json=_body(deepcopy(history)), headers=_HEADERS)
        assert retry.status_code == 200, f"retry {attempt + 1}: {retry.status_code} {retry.text[:300]}"
        assert _terminal_event_type(retry.text) == "response.completed"
    assert fleet.account_ids == [fleet.account_ids[0]] * 2
    assert len(fleet.frames(1)) == 3


@pytest.mark.asyncio
async def test_native_history_passes_an_open_retry_circuit_like_a_proof_gated_resend(
    async_client, app_instance, monkeypatch, caplog
):
    """Zero-event clean closes open the thread's retry circuit; the next complete history still goes out.

    The circuit admits the client's own replay-safe request while it cools
    down, as it admitted the verified unanchored body of an anchored native
    request. Unanchored, the next native turn was suppressed and, without a
    turn-state header, handed to raw HTTP instead of the bridge.
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000, plain_answers=True)
    _install_fleet(monkeypatch, fleet)
    # A raw fallback would arm the process-wide websocket failure marker.
    monkeypatch.setattr(proxy_support, "_upstream_ws_transport_failure_at", None)
    caplog.set_level(logging.INFO)
    raw_accounts: list[str] = []

    async def raw(payload, headers, access_token, account_id, **kwargs):
        raw_accounts.append(account_id)
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(proxy_module, "core_stream_responses", raw)
    first_turn, _tool_resend = _histories()
    async with AsyncClient(
        transport=ASGITransport(app=app_instance, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        first = await client.post(_ROUTE, json=_body(first_turn), headers=_HEADERS)
        assert _terminal_event_type(first.text) == "response.completed"
        history = [*first_turn, *_completed_output(first.text), _user("Next?")]
        fleet.drops = 100
        for _ in range(2):
            dropped = await client.post(_ROUTE, json=_body(deepcopy(history)), headers=_HEADERS)
            assert _terminal_event_type(dropped.text) == "response.failed"
        service = get_proxy_service_for_app(app_instance)

        async def circuit_open() -> None:
            while not any(
                state.consecutive_failures >= 2 and state.cooldown_until > 0
                for state in cast(Any, service)._http_bridge_retry_circuits.values()
            ):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(circuit_open(), timeout=5.0)
        fleet.drops = 0
        body = _body(deepcopy(history))
        response = await client.post(_ROUTE, json=body, headers=_HEADERS)

    assert response.status_code == 200, f"request failed: {response.status_code} {response.text[:300]}"
    assert _terminal_event_type(response.text) == "response.completed"
    assert raw_accounts == []
    _assert_complete_resend_on_owner(fleet.frames(len(fleet.sockets) - 1)[-1], client_body=body)
    assert set(fleet.account_ids) == {fleet.account_ids[0]}


@pytest.mark.parametrize("native", [True, False], ids=["native", "non_native"])
@pytest.mark.asyncio
async def test_large_native_history_is_not_a_warning_on_every_turn(async_client, monkeypatch, caplog, native):
    """A complete native history above the warning size logs at DEBUG; other clients keep the warning."""
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000)
    _install_fleet(monkeypatch, fleet)
    monkeypatch.setattr(proxy_module, "_UPSTREAM_RESPONSE_CREATE_WARN_BYTES", 64)
    headers = _HEADERS if native else {"session_id": "large-session", "thread-id": "large-thread"}
    first_turn, tool_resend = _histories()
    caplog.set_level(logging.DEBUG, logger="app.modules.proxy.service")
    await async_client.post(_ROUTE, json=_body(first_turn), headers=headers)
    response = await async_client.post(_ROUTE, json=_body(deepcopy(tool_resend)), headers=headers)

    assert _terminal_event_type(response.text) == "response.completed"
    large = [record for record in caplog.records if "Large response.create prepared" in record.getMessage()]
    assert large
    assert {record.levelno for record in large} == ({logging.DEBUG} if native else {logging.WARNING})


@pytest.mark.asyncio
async def test_owner_bound_native_tool_history_stays_on_its_owner_after_a_model_rejection(async_client, monkeypatch):
    """A model rejection does not move an owner-bound native history to another account.

    The owner produced a namespaced tool call, which only the owner can
    continue. The native history is unanchored, so its portability must be
    judged on the client's request: the wire text drops the namespace and
    looks portable. Before: the model retry and the reconnect judged that text
    and sent the call and its output to the other account.
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_two_accounts(async_client)
    fleet = _UpstreamFleet(close_code=1000)
    _install_fleet(monkeypatch, fleet)
    call = {
        "type": "function_call",
        "namespace": "collaboration",
        "name": "spawn_agent",
        "arguments": "{}",
        "call_id": "call_spawn",
    }
    monkeypatch.setitem(globals(), "_tool_turn_output", lambda: [call])
    initial = [_user("Delegate the task")]
    first = await async_client.post(_ROUTE, json=_body(initial), headers=_HEADERS)
    assert _terminal_event_type(first.text) == "response.completed"
    owner = fleet.sockets[0]

    async def reject_model(text: str) -> None:
        owner.sent_text.append(text)
        await owner._put(
            {
                "type": "error",
                "status": 400,
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_request_error",
                    "message": "The 'gpt-5.1' model is not supported when using Codex with a ChatGPT account.",
                },
            }
        )

    monkeypatch.setattr(owner, "send_text", reject_model)
    body = _body([*initial, call, {"type": "function_call_output", "call_id": "call_spawn", "output": "agent thread"}])
    # On the client's request the history is owner-bound.
    assert not anchor_free_replay_payload_is_account_neutral(
        proxy_module.ResponsesRequest.model_validate(body).to_replay_safety_payload(),
        strip_tool_search_ids=False,
    )
    await async_client.post(_ROUTE, json=body, headers=_HEADERS)

    assert set(fleet.account_ids) == {fleet.account_ids[0]}
    assert len(owner.sent_text) == 2
