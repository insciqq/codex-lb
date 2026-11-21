"""HTTP bridge continuity with developer messages kept inline in ``input``.

Developer messages used to be hoisted into ``instructions`` before the bridge
classified a request; they now stay in ``input`` (upstream issue 2563). The
bridge must still judge conversation history without them.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import anyio
import pytest

from app.core.clients.proxy_websocket import UpstreamWebSocket
from app.db.models import AccountStatus, HttpBridgeSessionState
from app.modules.proxy import service as proxy_service
from app.modules.proxy._service.http_bridge import streaming as http_bridge_streaming_module
from tests.unit.test_proxy_http_bridge import (  # noqa: F401  (autouse fixtures)
    _make_app_settings,
    _share_proxy_dashboard_settings,
    _stub_recovery_attempt_lookup,
)

pytestmark = pytest.mark.unit

_DEVELOPER: proxy_service.JsonValue = {
    "type": "message",
    "role": "developer",
    "content": [{"type": "input_text", "text": "<permissions instructions>sandbox</permissions instructions>"}],
}


_KINDS_METADATA: proxy_service.JsonValue = {"content_item_kinds": ["model.developer_instructions"]}
# Developer messages of shapes the strict proof predicate rejects; the
# delta-or-full-resend classifiers must still ignore every one of them.
_DEVELOPER_SHAPES = [
    pytest.param(_DEVELOPER, id="plain"),
    pytest.param(
        {**cast(dict[str, Any], _DEVELOPER), "internal_chat_message_metadata_passthrough": _KINDS_METADATA},
        id="content-kinds-metadata",
    ),
    pytest.param(
        {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "perm"}, {"type": "input_text", "text": " "}],
        },
        id="blank-part",
    ),
    pytest.param({**cast(dict[str, Any], _DEVELOPER), "status": "in_progress", "name": "x"}, id="extra-keys"),
]


def _request_state(request_id: str, *, previous_response_id: str | None = None) -> proxy_service._WebSocketRequestState:
    return proxy_service._WebSocketRequestState(
        request_id=request_id,
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
        previous_response_id=previous_response_id,
    )


def _session(key: proxy_service._HTTPBridgeSessionKey, affinity: proxy_service._AffinityPolicy) -> Any:
    return proxy_service._HTTPBridgeSession(
        key=key,
        headers={"x-codex-session-id": "sid-123"} if key.affinity_kind == "session_header" else {},
        affinity=affinity,
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )


def _codex_session() -> Any:
    return _session(
        proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
        proxy_service._AffinityPolicy(key="sid-123", kind=proxy_service.StickySessionKind.CODEX_SESSION),
    )


def _patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())


def _durable_lookup(**overrides: Any) -> proxy_service.DurableBridgeLookup:
    values: dict[str, Any] = {
        "session_id": "sess-1",
        "canonical_kind": "session_header",
        "canonical_key": "sid-123",
        "api_key_scope": "__anonymous__",
        "account_id": "acc-1",
        "owner_instance_id": "instance-a",
        "owner_epoch": 1,
        "lease_expires_at": datetime.now(timezone.utc),
        "state": HttpBridgeSessionState.ACTIVE,
        "latest_turn_state": "http_turn_1",
        "latest_response_id": "resp_latest",
    }
    values.update(overrides)
    return proxy_service.DurableBridgeLookup(**values)


async def _drain(service: Any, payload: proxy_service.ResponsesRequest, *, codex: bool) -> list[str]:
    return [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={"x-codex-session-id": "sid-123"} if codex else {},
            codex_session_affinity=codex,
            propagate_http_errors=False,
            openai_cache_affinity=not codex,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=1800.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("developer", _DEVELOPER_SHAPES)
async def test_cold_bridge_keeps_durable_anchor_for_developer_and_user_delta(
    monkeypatch: pytest.MonkeyPatch, developer: proxy_service.JsonValue
) -> None:
    """``[developer, user]`` is a turn delta: the reattach keeps the owner's anchor."""
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": [developer, {"role": "user", "content": "continue"}]},
    )
    request_state = _request_state("req-developer-delta")
    assert request_state.event_queue is not None
    await request_state.event_queue.put(None)
    prepared: list[proxy_service.ResponsesRequest] = []

    def fake_prepare(prepared_payload: proxy_service.ResponsesRequest, _headers: Any, **kwargs: Any) -> Any:
        del kwargs
        prepared.append(prepared_payload)
        return request_state, '{"type":"response.create"}'

    session = _codex_session()
    _patch_settings(monkeypatch)
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", AsyncMock(return_value=_durable_lookup()))
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=session))
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    assert await _drain(service, payload, codex=True) == []

    assert prepared[-1].previous_response_id == "resp_latest"
    assert prepared[-1].input == payload.input


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored_input_items",
    [
        pytest.param([_DEVELOPER, {"role": "user", "content": "hello"}], id="developer-and-user"),
        pytest.param(
            [
                {**cast(dict[str, Any], _DEVELOPER), "internal_chat_message_metadata_passthrough": _KINDS_METADATA},
                {"role": "user", "content": "hello"},
            ],
            id="developer-with-content-kinds-and-user",
        ),
        pytest.param([_DEVELOPER], id="developer-only"),
    ],
)
async def test_fresh_bridge_preserves_non_lite_full_resend_with_developer_prefix(
    monkeypatch: pytest.MonkeyPatch, stored_input_items: list[proxy_service.JsonValue]
) -> None:
    """A verified non-Lite full resend goes unanchored, as when developer text was hoisted.

    Sending it with the durable anchor would repeat the prior assistant output
    that the anchor already carries.
    """
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    suffix_items: list[proxy_service.JsonValue] = [
        {"role": "assistant", "content": "hello back"},
        {"role": "user", "content": "follow up"},
    ]
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": [*stored_input_items, *suffix_items]},
    )
    request_state = _request_state("req-developer-full-resend")
    assert request_state.event_queue is not None
    await request_state.event_queue.put(None)
    dispatched: list[dict[str, Any]] = []
    real_prepare = service._prepare_http_bridge_request

    def fake_prepare(prepared_payload: proxy_service.ResponsesRequest, _headers: Any, **kwargs: Any) -> Any:
        _, text_data = real_prepare(prepared_payload, _headers, **kwargs)
        dispatched.append(json.loads(text_data))
        request_state.previous_response_id = prepared_payload.previous_response_id
        return request_state, text_data

    session = _codex_session()
    session.codex_session = True
    _patch_settings(monkeypatch)
    monkeypatch.setattr(
        service._durable_bridge,
        "lookup_request_targets",
        AsyncMock(
            return_value=_durable_lookup(
                latest_input_item_count=len(stored_input_items),
                latest_input_full_fingerprint=proxy_service._fingerprint_input_items(stored_input_items),
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        ),
    )
    monkeypatch.setattr(
        http_bridge_streaming_module, "_http_bridge_payload_is_account_neutral_fresh_replay", Mock(return_value=True)
    )
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_http_bridge_can_forward_to_active_owner", AsyncMock(return_value=False))
    monkeypatch.setattr(service, "_resolve_websocket_previous_response_owner", AsyncMock(return_value="acc-1"))
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=session))
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    assert await _drain(service, payload, codex=True) == []

    assert dispatched, "the request was never prepared"
    assert all("previous_response_id" not in frame for frame in dispatched)
    assert dispatched[-1]["input"] == payload.input


@pytest.mark.asyncio
async def test_anchored_tool_continuation_trims_echoed_output_around_a_developer_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A developer update between the echoed prior output and the tool result stays; the echo goes."""
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    tool_output: proxy_service.JsonValue = {
        "type": "function_call_output",
        "call_id": "call_repeat",
        "output": "Wed May 6 16:00:00 UTC 2026",
    }
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "hi",
            "previous_response_id": "resp_prev_tool_call",
            "input": [
                {"id": "rs_repeat", "type": "reasoning", "summary": []},
                {
                    "id": "msg_repeat",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "running command"}],
                },
                {
                    "id": "fc_repeat",
                    "type": "function_call",
                    "call_id": "call_repeat",
                    "name": "exec_command",
                    "arguments": '{"cmd":"date"}',
                },
                _DEVELOPER,
                tool_output,
            ],
        }
    )
    request_state = _request_state("req-trim-developer-update")
    assert request_state.event_queue is not None
    await request_state.event_queue.put(None)
    prepared: list[proxy_service.ResponsesRequest] = []

    def fake_prepare(prepared_payload: proxy_service.ResponsesRequest, _headers: Any, **kwargs: Any) -> Any:
        del kwargs
        prepared.append(prepared_payload)
        request_state.previous_response_id = prepared_payload.previous_response_id
        return request_state, json.dumps({"type": "response.create", "input": prepared_payload.input})

    session = _codex_session()
    _patch_settings(monkeypatch)
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_resolve_websocket_previous_response_owner", AsyncMock(return_value="acc-1"))
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=session))
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    assert await _drain(service, payload, codex=True) == []

    assert prepared[-1].previous_response_id == "resp_prev_tool_call"
    assert prepared[-1].input == [_DEVELOPER, tool_output]


@pytest.mark.asyncio
async def test_trimmed_developer_only_prefix_does_not_make_the_delta_replayable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trimming a stored prefix of developer messages leaves a delta, as before they stayed inline.

    The previous turn sent only a developer message; its answer lives in the
    anchor. The unanchored body ``[developer, question]`` lacks that answer, so
    a pre-create retry must not send it without the anchor.
    """
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    # The stored context is the previous request's input as normalized.
    previous_request = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": [_DEVELOPER]},
    )
    stored_input_items = cast(list[proxy_service.JsonValue], previous_request.input)
    question: proxy_service.JsonValue = {"role": "user", "content": "explain your previous answer"}
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": [*stored_input_items, question]},
    )
    request_state = _request_state("req-developer-prefix-trim")
    assert request_state.event_queue is not None
    await request_state.event_queue.put(None)
    prepared: list[proxy_service.ResponsesRequest] = []

    def fake_prepare(prepared_payload: proxy_service.ResponsesRequest, _headers: Any, **kwargs: Any) -> Any:
        del kwargs
        prepared.append(prepared_payload)
        return request_state, '{"type":"response.create"}'

    session = _codex_session()
    session.codex_session = True
    session.last_completed_response_id = "resp_session_latest"
    session.last_completed_response_account_id = "acc-1"
    if stored_input_items:
        session.last_completed_input_count = len(stored_input_items)
        session.last_completed_input_prefix_fingerprint = proxy_service._fingerprint_input_items(stored_input_items)
    _patch_settings(monkeypatch)
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=session))
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    assert await _drain(service, payload, codex=True) == []

    assert prepared[-1].previous_response_id == "resp_session_latest"
    assert prepared[-1].input == [question]
    assert request_state.fresh_upstream_request_is_retry_safe is False
