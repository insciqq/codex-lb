from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256

from app.core.openai.requests import ResponsesRequest, upstream_hoist_view
from app.core.types import JsonValue
from app.modules.proxy.affinity import (
    _prompt_cache_key_from_request_model,
    _sticky_key_from_session_header,
    _sticky_key_from_turn_state_header,
)

_TOOL_RESULTS = frozenset({"function_call_output", "custom_tool_call_output", "apply_patch_call_output"})


def http_continuation_signal(payload: ResponsesRequest, headers: Mapping[str, str]) -> str | None:
    """Classify supplied evidence, before affinity derives a cache key."""
    if payload.previous_response_id is not None:
        return "previous_response"
    if payload.conversation and payload.conversation.strip():
        return "conversation"
    if _sticky_key_from_turn_state_header(headers) is not None:
        return "turn_state"
    if _sticky_key_from_session_header(headers) is not None:
        return "session"
    if _prompt_cache_key_from_request_model(payload) is not None:
        return "prompt_cache"
    return http_history_signal(payload)


def http_history_signal(payload: ResponsesRequest) -> str | None:
    # Only a request without its own key reaches this check (see
    # ``http_continuation_signal``), so judge the items upstream main's hoist
    # left in ``input``, as the inferred key does.
    return _history_signal(upstream_hoist_view(payload)[1])


def _history_signal(items: JsonValue) -> str | None:
    if not isinstance(items, list):
        return None
    assistant_seen = False
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if isinstance(item_type, str) and item_type in _TOOL_RESULTS:
            return "tool_result"
        if item.get("role") == "assistant":
            assistant_seen = True
        elif item.get("role") == "user" and assistant_seen:
            return "history"
    return None


def inferred_http_bridge_key(payload: ResponsesRequest) -> str | None:
    """Soft connection locality only; this key is never a continuity proof.

    API-key isolation is supplied by _HTTPBridgeSessionKey. Hash complete
    initial context, not the truncated prompt-cache prefix. Requests sharing
    that context may share a socket, but must send their own complete history.
    """
    if payload.conversation and payload.conversation.strip():
        identity = [payload.model, payload.conversation]
        kind = "conversation"
    else:
        # Instructions, history and initial context as upstream main's hoist left them.
        instructions, identity_input = upstream_hoist_view(payload)
        if _history_signal(identity_input) is None or not isinstance(identity_input, list):
            return None
        initial_context = []
        for item in identity_input:
            if not isinstance(item, dict):
                continue
            if item.get("role") in ("system", "developer", "user"):
                initial_context.append(item)
            if item.get("role") == "user":
                break
        else:
            # A delta-only tool result cannot identify its conversation.
            return None
        identity = [payload.model, instructions, initial_context]
        kind = "history"
    encoded = json.dumps(identity, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    return f"http-{kind}:{sha256(encoded).hexdigest()}"
