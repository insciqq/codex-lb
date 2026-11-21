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

from app.core.clients.proxy import ProxyResponseError
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
        pytest.param(
            [
                {
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "rules"}, {"type": "input_text", "text": " "}],
                },
                {"role": "user", "content": "hello"},
            ],
            id="developer-with-blank-part-and-user",
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
        {"role": "assistant", "content": "hello back", "id": "msg_prior"},
        {"role": "user", "content": "follow up"},
    ]
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": [*stored_input_items, *suffix_items]},
    )
    # The durable context is what the bridge recorded for the stored turn.
    stored_request = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": stored_input_items},
    )
    stored_count, stored_fingerprint = proxy_service._input_context(
        cast(list[proxy_service.JsonValue], stored_request.input)
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
                latest_input_item_count=stored_count or None,
                latest_input_full_fingerprint=stored_fingerprint if stored_count else None,
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
async def test_developer_only_previous_turn_leaves_no_stored_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """A previous turn of developer messages alone stores no context, as when they were hoisted.

    The bridge therefore neither trims nor anchors the next turn on that
    context, and the unanchored body is not promoted to a safe retry.
    """
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    previous_request = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": [_DEVELOPER]},
    )
    stored_count, stored_fingerprint = proxy_service._input_context(
        cast(list[proxy_service.JsonValue], previous_request.input)
    )
    assert stored_count == 0
    question: proxy_service.JsonValue = {"role": "user", "content": "explain your previous answer"}
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": [_DEVELOPER, question]},
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
    _patch_settings(monkeypatch)
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=session))
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    assert await _drain(service, payload, codex=True) == []

    assert prepared[-1].input == payload.input
    assert request_state.fresh_upstream_request_is_retry_safe is False


@pytest.mark.asyncio
async def test_prepended_developer_message_keeps_the_warm_session_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer message placed ahead of the stored history keeps the stored-prefix match and goes upstream.

    The anchor predates the new instruction, so it cannot supply it.
    """
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    prefix_items: list[proxy_service.JsonValue] = [
        {"role": "user", "content": [{"type": "input_text", "text": "a"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "b"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "c"}]},
    ]
    new_turn: proxy_service.JsonValue = {"role": "user", "content": [{"type": "input_text", "text": "d"}]}
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "hi",
            "input": [{"role": "developer", "content": "Answer in Spanish"}, *prefix_items, new_turn],
        },
    )
    request_state = _request_state("req-prepended-developer")
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
    session.last_completed_input_count = len(prefix_items)
    session.last_completed_input_prefix_fingerprint = proxy_service._fingerprint_input_items(prefix_items)
    _patch_settings(monkeypatch)
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=session))
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    assert await _drain(service, payload, codex=True) == []

    assert prepared[-1].previous_response_id == "resp_session_latest"
    assert prepared[-1].input == [{"role": "developer", "content": "Answer in Spanish"}, new_turn]


def test_developer_update_does_not_split_replayed_tool_call_dedupe() -> None:
    """A developer message the old hoist removed is no segment boundary for replayed tool calls."""
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    call: proxy_service.JsonValue = {
        "type": "function_call",
        "name": "write_stdin",
        "arguments": json.dumps({"session_id": 75180, "chars": "", "yield_time_ms": 30000}),
        "call_id": "call_first",
    }
    output: proxy_service.JsonValue = {
        "type": "function_call_output",
        "call_id": "call_first",
        "output": "Process running with session ID 75180",
    }
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.1",
            "instructions": "",
            "previous_response_id": "resp_anchor",
            "input": [
                {"role": "user", "content": "check the terminal"},
                call,
                output,
                {"role": "developer", "content": "updated instructions"},
                call,
                output,
            ],
        }
    )

    _, text_data = service._prepare_response_bridge_request_state(
        payload,
        api_key=None,
        api_key_reservation=None,
        include_type_field=True,
        attach_event_queue=False,
        transport=proxy_service._REQUEST_TRANSPORT_WEBSOCKET,
        client_metadata=None,
    )

    upstream_input = json.loads(text_data)["input"]
    assert sum(item.get("type") == "function_call" for item in upstream_input) == 1
    assert {"role": "developer", "content": "updated instructions"} in upstream_input


@pytest.mark.asyncio
async def test_repeated_developer_instruction_is_no_proof_for_cross_transport_failover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WebSocket turn of one developer message, then an HTTP turn repeating it, stays on the owner.

    The repeated developer message carries no history: the prior answer lives
    only in the anchor, so the request must not move to another account
    without it.
    """
    from tests.unit.test_proxy_utils import (
        AccountSelection,
        _make_account,
        _make_proxy_settings,
        _repo_factory,
        _RequestLogsRecorder,
        _SettingsCache,
    )

    settings = _make_proxy_settings()
    request_logs = _RequestLogsRecorder()
    service = proxy_service.ProxyService(_repo_factory(request_logs))
    owner_account = _make_account("acc_dev_replay_owner")
    replacement_account = _make_account("acc_dev_replay_replacement")
    session_id = "sid_dev_cross_transport"
    previous_response_id = "resp_dev_cross_transport"
    request_logs.response_owner_by_id[(previous_response_id, None, session_id)] = owner_account.id
    developer: proxy_service.JsonValue = {"role": "developer", "content": "Describe a new design"}
    initial_input = cast(
        list[proxy_service.JsonValue],
        proxy_service.ResponsesRequest(model="m", instructions="", input=[developer]).input,
    )
    service._websocket_continuity_index[(session_id, None)] = proxy_service._WebSocketContinuityState(
        last_completed_response_id=previous_response_id,
        last_completed_input_count=len(initial_input),
        last_completed_input_prefix_fingerprint=proxy_service._fingerprint_input_items(initial_input),
    )
    streamed_payloads: list[proxy_service.ResponsesRequest] = []

    async def fake_select_account(**kwargs: Any) -> Any:
        if kwargs.get("required_account_id") == owner_account.id:
            return AccountSelection(account=owner_account, error_message=None)
        return AccountSelection(account=replacement_account, error_message=None)

    async def fake_stream(payload: Any, headers: Any, access_token: Any, account_id: Any, **kwargs: Any):
        del headers, access_token, kwargs
        streamed_payloads.append(payload)
        if account_id == owner_account.chatgpt_account_id:
            yield (
                'data: {"type":"response.failed","response":{"id":"resp_owner_quota",'
                '"status":"failed","error":{"code":"usage_limit_reached",'
                '"message":"usage limit reached"},"usage":{"input_tokens":0,'
                '"output_tokens":0,"total_tokens":0}}}\n\n'
            )
            return
        yield (
            'data: {"type":"response.completed","response":{"id":"resp_replay_ok",'
            '"status":"completed","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n'
        )

    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _SettingsCache(settings))
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(service._load_balancer, "select_account", AsyncMock(side_effect=fake_select_account))
    monkeypatch.setattr(service._load_balancer, "record_success", AsyncMock())
    monkeypatch.setattr(service, "_handle_stream_error", AsyncMock(return_value={"failure_class": "rate_limit"}))
    monkeypatch.setattr(service, "_resolve_upstream_route_for_account", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_ensure_fresh", AsyncMock(side_effect=lambda account, **kwargs: account))
    monkeypatch.setattr(service, "_settle_stream_api_key_usage", AsyncMock(return_value=True))
    monkeypatch.setattr(proxy_service, "core_stream_responses", fake_stream)
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.6-sol",
            "instructions": "",
            "input": [developer, {"role": "user", "content": "Revise your previous design"}],
            "previous_response_id": previous_response_id,
            "stream": True,
        }
    )

    [chunk async for chunk in service.stream_responses(payload, {"session_id": session_id})]

    assert [streamed.previous_response_id for streamed in streamed_payloads] == [previous_response_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("json_mode", "expected_previous_response_ids"),
    [
        pytest.param(False, ["resp_dev_id_cross_transport", None], id="hoisted-id-is-dropped"),
        # The raw reasoning part mentions JSON, so the old hoist kept the
        # message (and its id) in ``input``; sanitization then removes that
        # part. The id is on the frame the replacement would receive.
        pytest.param(True, ["resp_dev_id_cross_transport"], id="json-kept-id-stays-with-owner"),
    ],
)
async def test_developer_item_id_does_not_strand_a_verified_cross_transport_failover(
    monkeypatch: pytest.MonkeyPatch,
    json_mode: bool,
    expected_previous_response_ids: list[str | None],
) -> None:
    """A complete transcript whose only item id sits on a developer message fails over as before.

    The hoist dropped that id with a message it moved; it now never reaches
    upstream, so the replacement body passes the account-neutrality gate. An id
    the hoist kept is judged on the actual frame and keeps the turn on its owner.
    """
    from tests.unit.test_proxy_utils import (
        AccountSelection,
        _make_account,
        _make_proxy_settings,
        _repo_factory,
        _RequestLogsRecorder,
        _SettingsCache,
    )

    settings = _make_proxy_settings()
    request_logs = _RequestLogsRecorder()
    service = proxy_service.ProxyService(_repo_factory(request_logs))
    owner_account = _make_account("acc_dev_id_owner")
    replacement_account = _make_account("acc_dev_id_replacement")
    session_id = "sid_dev_id_cross_transport"
    previous_response_id = "resp_dev_id_cross_transport"
    request_logs.response_owner_by_id[(previous_response_id, None, session_id)] = owner_account.id
    developer: proxy_service.JsonValue = {
        "type": "message",
        "role": "developer",
        "id": "msg_rules",
        "content": (
            [{"type": "reasoning", "text": "Use JSON"}, {"type": "input_text", "text": "Use metric."}]
            if json_mode
            else "Keep rules"
        ),
    }
    initial_input: list[proxy_service.JsonValue] = [{"role": "user", "content": "first turn"}]
    service._websocket_continuity_index[(session_id, None)] = proxy_service._WebSocketContinuityState(
        last_completed_response_id=previous_response_id,
        last_completed_input_count=len(initial_input),
        last_completed_input_prefix_fingerprint=proxy_service._fingerprint_input_items(initial_input),
    )
    streamed_payloads: list[proxy_service.ResponsesRequest] = []

    async def fake_select_account(**kwargs: Any) -> Any:
        if kwargs.get("required_account_id") == owner_account.id:
            return AccountSelection(account=owner_account, error_message=None)
        return AccountSelection(account=replacement_account, error_message=None)

    async def fake_stream(payload: Any, headers: Any, access_token: Any, account_id: Any, **kwargs: Any):
        del headers, access_token, kwargs
        streamed_payloads.append(payload)
        if account_id == owner_account.chatgpt_account_id:
            yield (
                'data: {"type":"response.failed","response":{"id":"resp_owner_quota",'
                '"status":"failed","error":{"code":"usage_limit_reached",'
                '"message":"usage limit reached"},"usage":{"input_tokens":0,'
                '"output_tokens":0,"total_tokens":0}}}\n\n'
            )
            return
        yield (
            'data: {"type":"response.completed","response":{"id":"resp_replay_ok",'
            '"status":"completed","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n'
        )

    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _SettingsCache(settings))
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(service._load_balancer, "select_account", AsyncMock(side_effect=fake_select_account))
    monkeypatch.setattr(service._load_balancer, "record_success", AsyncMock())
    monkeypatch.setattr(service, "_handle_stream_error", AsyncMock(return_value={"failure_class": "rate_limit"}))
    monkeypatch.setattr(service, "_resolve_upstream_route_for_account", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_ensure_fresh", AsyncMock(side_effect=lambda account, **kwargs: account))
    monkeypatch.setattr(service, "_settle_stream_api_key_usage", AsyncMock(return_value=True))
    monkeypatch.setattr(proxy_service, "core_stream_responses", fake_stream)
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.6-sol",
            "instructions": "",
            "input": [
                *initial_input,
                developer,
                {"role": "assistant", "content": [{"type": "output_text", "text": "Prior answer"}]},
                {"role": "user", "content": "Continue in JSON" if json_mode else "Continue"},
            ],
            "previous_response_id": previous_response_id,
            "stream": True,
            **({"text": {"format": {"type": "json_object"}}} if json_mode else {}),
        }
    )

    [chunk async for chunk in service.stream_responses(payload, {"session_id": session_id})]

    assert [streamed.previous_response_id for streamed in streamed_payloads] == expected_previous_response_ids
    if len(streamed_payloads) > 1:
        replayed_developer = cast(list[dict[str, Any]], streamed_payloads[1].input)[1]
        assert replayed_developer["role"] == "developer" and "id" not in replayed_developer


# Frames the base normalizer (before developer messages stayed inline)
# produced for these inputs through ``_prepare_http_bridge_request``; captured
# from that revision, not rebuilt by the helper under test.
_PRE_UPGRADE_CONTINUE = {"role": "user", "content": "continue"}
_PRE_UPGRADE_FRAMES = {
    "developer": (
        [{"role": "developer", "content": "review only"}, _PRE_UPGRADE_CONTINUE],
        '{"model":"gpt-5.6","instructions":"review only","input":[{"role":"user","content":"continue"}],'
        '"store":false,"include":[],"previous_response_id":"resp-parent","type":"response.create"}',
    ),
    "developer-then-system": (
        [{"role": "developer", "content": "D"}, {"role": "system", "content": "S"}, _PRE_UPGRADE_CONTINUE],
        '{"model":"gpt-5.6","instructions":"D\\nS","input":[{"role":"user","content":"continue"}],'
        '"store":false,"include":[],"previous_response_id":"resp-parent","type":"response.create"}',
    ),
    "blank-developer": (
        [{"role": "developer", "content": "  "}, {"role": "developer", "content": "D"}, _PRE_UPGRADE_CONTINUE],
        '{"model":"gpt-5.6","instructions":"  \\nD","input":[{"role":"user","content":"continue"}],'
        '"store":false,"include":[],"previous_response_id":"resp-parent","type":"response.create"}',
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_PRE_UPGRADE_FRAMES))
async def test_retry_of_a_pre_upgrade_operation_finds_its_record(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    """An operation recorded before developer messages stayed inline still fences its identical retry.

    Its key hashed the frame the old normalizer built (developer and system
    text in ``instructions``, in input order); the retry must find that row
    instead of dispatching the turn a second time.
    """
    from app.modules.proxy.durable_bridge_repository import (
        durable_bridge_api_key_scope,
        durable_bridge_operation_fingerprint,
        durable_bridge_operation_id,
    )
    from tests.unit.test_proxy_http_bridge import _make_bridge_session

    raw_input, legacy_text = _PRE_UPGRADE_FRAMES[case]
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    send_text = AsyncMock()
    session = _make_bridge_session(key_value="pre-upgrade-operation")
    session.upstream = cast(UpstreamWebSocket, SimpleNamespace(send_text=send_text, close=AsyncMock()))
    session.durable_session_id = "durable-pre-upgrade-operation"
    session.durable_owner_epoch = 4
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.6", "instructions": "", "previous_response_id": "resp-parent", "input": raw_input}
    )
    request_state, request_text = service._prepare_http_bridge_request(
        payload, {}, api_key=None, api_key_reservation=None
    )
    assert request_text != legacy_text
    request_state.request_id = "req-pre-upgrade-operation"
    request_state.hard_continuity_anchor = True
    request_state.skip_request_log = True
    legacy_fingerprint = durable_bridge_operation_fingerprint(
        api_key_scope=durable_bridge_api_key_scope(session.key.api_key_id), request_text=legacy_text
    )
    legacy_operation_id = durable_bridge_operation_id(session.durable_session_id, legacy_fingerprint)
    abandoned = SimpleNamespace(
        operation_id=legacy_operation_id,
        session_id=session.durable_session_id,
        state="abandoned",
        created=False,
        event_spool_complete=False,
        response_id=None,
    )
    fresh = SimpleNamespace(
        operation_id="operation-new-format",
        session_id=session.durable_session_id,
        state="submitted",
        created=True,
        event_spool_complete=False,
        response_id=None,
    )

    async def get_operation_by_fingerprint(*, request_fingerprint: str, **kwargs: Any) -> Any:
        del kwargs
        return abandoned if request_fingerprint == legacy_fingerprint else None

    async def get_operation(*, operation_id: str) -> Any:
        return abandoned if operation_id == legacy_operation_id else None

    async def record_operation(*, request_fingerprint: str, **kwargs: Any) -> Any:
        del kwargs
        return abandoned if request_fingerprint == legacy_fingerprint else fresh

    service._durable_bridge = cast(
        Any,
        SimpleNamespace(
            get_operation_by_fingerprint=get_operation_by_fingerprint,
            get_operation=get_operation,
            record_operation=record_operation,
            claim_unknown_operation_for_recovery=AsyncMock(),
            rollback_recovery_attempt_before_dispatch=AsyncMock(return_value=True),
        ),
    )
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: _make_app_settings(http_responses_session_bridge_instance_id="instance-pre-upgrade-operation"),
    )
    monkeypatch.setattr(service, "_http_bridge_precreated_retry_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "_http_bridge_precreated_retry_cooldown_seconds", AsyncMock(return_value=0.0))

    with pytest.raises(ProxyResponseError) as exc_info:
        await service._submit_http_bridge_request_with_handoff(
            session,
            request_state=request_state,
            text_data=request_text,
            queue_limit=8,
            request_scope_id="scope-pre-upgrade-operation",
            owned_unanchored_handoff=False,
        )

    # The abandoned pre-upgrade row is found: full-history recovery, no send.
    assert exc_info.value.status_code == 400
    assert exc_info.value.payload["error"]["code"] == "previous_response_not_found"
    send_text.assert_not_awaited()
