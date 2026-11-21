"""An anchored local rebind resends what the first dispatch sent.

When a request carries an anchor (the client's ``previous_response_id`` or one
the bridge added) and repeats the anchored turn's input, the bridge trims that
stored prefix and sends only the rest. If upstream rejects the anchor before
``response.created`` and the request has no proof that it can be replayed
without the anchor, the bridge rebinds locally and retries with the same
anchor. The retry used to send the untrimmed body, so upstream received the
stored prefix twice: once through the anchor and once inline.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import app.modules.proxy.service as proxy_module
from app.core.clients.proxy_websocket import UpstreamWebSocketMessage
from app.dependencies import get_proxy_service_for_app
from tests.integration.test_http_responses_bridge import (
    _cleanup_http_bridge_sessions as cleanup_http_bridge_sessions,  # noqa: F401
)
from tests.integration.test_http_responses_bridge import (
    _FakeBridgeUpstreamWebSocket,
    _import_account,
    _install_bridge_settings,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]

_ROUTE = "/backend-api/codex/responses"


def _user(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


class _RejectingUpstream(_FakeBridgeUpstreamWebSocket):
    """Answers ``previous_response_not_found`` to its second frame, before ``response.created``."""

    def __init__(self, prefix: str, *, reject_second: bool, first_output: list[dict[str, Any]] | None) -> None:
        super().__init__(prefix)
        self.reject_second = reject_second
        self.first_output = first_output

    async def _put(self, event: dict[str, Any]) -> None:
        await self._messages.put(UpstreamWebSocketMessage("text", text=json.dumps(event)))

    async def send_text(self, text: str) -> None:
        if self.first_output is not None and not self.sent_text:
            self.sent_text.append(text)
            response = {"id": f"{self.response_id_prefix}_1", "object": "response", "status": "in_progress"}
            await self._put({"type": "response.created", "response": response})
            for index, item in enumerate(self.first_output):
                await self._put({"type": "response.output_item.done", "output_index": index, "item": item})
            completed = {**response, "status": "completed", "output": self.first_output}
            await self._put({"type": "response.completed", "response": completed})
            return
        if not (self.reject_second and len(self.sent_text) == 1):
            await super().send_text(text)
            return
        self.sent_text.append(text)
        anchor = json.loads(text).get("previous_response_id")
        error = {
            "type": "invalid_request_error",
            "code": "previous_response_not_found",
            "message": f"Previous response with id '{anchor}' not found.",
            "param": "previous_response_id",
        }
        await self._put({"type": "error", "status": 400, "error": error})


_PENDING_CALL = {"type": "function_call", "call_id": "call_a", "name": "shell", "arguments": "{}"}


_BODY = {"model": "gpt-5.1", "instructions": "Be brief.", "stream": True, "prompt_cache_key": "rebind-thread"}
_HISTORY = [_user("First question."), _user("Second question.")]


async def _install_upstreams(
    async_client, monkeypatch, *, first_output: list[dict[str, Any]] | None = None
) -> list[_RejectingUpstream]:
    _install_bridge_settings(monkeypatch, enabled=True)
    for name in ("acc_anchored_rebind_a", "acc_anchored_rebind_b"):
        await _import_account(async_client, name, f"{name}@example.com")
    sockets: list[_RejectingUpstream] = []

    async def fresh(self, target, *, force=False, timeout_seconds):
        del self, force, timeout_seconds
        return target

    async def connect(headers, access_token, account_id_header, *, base_url=None, session=None):
        del headers, access_token, account_id_header, base_url, session
        first = not sockets
        upstream = _RejectingUpstream(
            f"resp_conn{len(sockets) + 1}", reject_second=first, first_output=first_output if first else None
        )
        sockets.append(upstream)
        return upstream

    monkeypatch.setattr(proxy_module.ProxyService, "_ensure_fresh_with_budget", fresh)
    monkeypatch.setattr(proxy_module, "connect_responses_websocket", connect)
    return sockets


@pytest.mark.asyncio
@pytest.mark.parametrize("first_output", [None, [_PENDING_CALL]], ids=["answered", "interrupted-tool-call"])
async def test_anchored_local_rebind_resends_the_trimmed_request(async_client, monkeypatch, first_output):
    """The retry sends what the first dispatch sent and keeps its stored context."""
    sockets = await _install_upstreams(async_client, monkeypatch, first_output=first_output)

    first = await async_client.post(_ROUTE, json={**_BODY, "input": _HISTORY[:1]})
    second = await async_client.post(_ROUTE, json={**_BODY, "input": _HISTORY, "previous_response_id": "resp_conn1_1"})
    third = await async_client.post(
        _ROUTE,
        json={**_BODY, "input": [*_HISTORY, _user("Third question.")], "previous_response_id": "resp_conn2_1"},
    )

    assert [first.status_code, second.status_code, third.status_code] == [200, 200, 200]
    assert [len(upstream.sent_text) for upstream in sockets] == [2, 2]
    dispatched, retry = json.loads(sockets[0].sent_text[1]), json.loads(sockets[1].sent_text[0])
    assert dispatched["previous_response_id"] == retry["previous_response_id"] == "resp_conn1_1"
    assert retry["input"] == dispatched["input"]
    # The stored prefix went through the anchor only; an interrupted call
    # keeps the output the bridge added for it.
    assert dispatched["input"][-1] == _user("Second question.")
    assert _user("First question.") not in dispatched["input"]
    assert [item.get("call_id") for item in dispatched["input"][:-1]] == (["call_a"] if first_output else [])
    # The retry stored the full client input as the session context, so the
    # next turn still trims against it.
    following = json.loads(sockets[1].sent_text[1])
    assert following["previous_response_id"] == "resp_conn2_1"
    assert following["input"] == [_user("Third question.")]


@pytest.mark.asyncio
async def test_anchored_local_rebind_keeps_the_anchor_a_sibling_advanced_to(async_client, app_instance, monkeypatch):
    """The operation lookup moved the dispatch to a sibling's response; the retry stays on it."""
    sockets = await _install_upstreams(async_client, monkeypatch)
    first = await async_client.post(_ROUTE, json={**_BODY, "input": _HISTORY[:1]})
    assert first.status_code == 200
    durable = get_proxy_service_for_app(app_instance)._durable_bridge
    completed = [SimpleNamespace(response_id="resp_sibling")]

    async def latest_completed_operation(**kwargs: Any) -> SimpleNamespace | None:
        del kwargs
        return completed.pop() if completed else None

    monkeypatch.setattr(durable, "get_latest_completed_operation", latest_completed_operation)

    second = await async_client.post(_ROUTE, json={**_BODY, "input": _HISTORY, "previous_response_id": "resp_conn1_1"})

    assert second.status_code == 200
    assert [len(upstream.sent_text) for upstream in sockets] == [2, 1]
    dispatched, retry = json.loads(sockets[0].sent_text[1]), json.loads(sockets[1].sent_text[0])
    assert dispatched["previous_response_id"] == retry["previous_response_id"] == "resp_sibling"
    assert retry["input"] == dispatched["input"] == [_user("Second question.")]
