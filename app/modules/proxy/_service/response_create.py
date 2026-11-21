from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import sys
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, TypeVar, cast
from uuid import uuid4

from app.core.clients.proxy import (
    _AGENT_CONTROL_OUTPUT_ITEM_TYPES,
    _RESPONSE_CREATE_TOOL_OUTPUT_OMISSION_NOTICE,
    CODEX_INSTALLATION_ID_HEADER,
    UPSTREAM_RESPONSE_CREATE_MAX_BYTES,
    ImageFetchSession,
    ProxyResponseError,
    _agent_control_tool_output_occurrences,
    _finalize_responses_lite_reasoning_context,
    _historical_agent_control_output_occurrences,
    _inline_content_images,
    _is_inline_image_reference,
    _normalize_responses_lite_websocket_client_metadata,
    _payload_has_responses_lite_websocket_marker,
    _payload_uses_responses_lite,
    _response_create_inline_image_notice_item,
    _response_create_recent_suffix_start,
    _response_create_too_large_error_envelope,
    _should_slim_historical_tool_output,
    _slim_historical_response_content,
    _ws_transport_payload_budget_bytes,
    apply_codex_installation_metadata,
)
from app.core.clock import REAL_SCHEDULER, Scheduler
from app.core.config.settings import DEFAULT_HOME_DIR, get_settings
from app.core.openai.requests import (
    NO_BASE_HOIST,
    BaseHoist,
    ResponsesRequest,
    base_equivalent_input,
    base_equivalent_input_positions,
)
from app.core.types import JsonValue
from app.core.utils.json_guards import is_json_mapping
from app.core.utils.payload_size import compact_json_dumps_within, utf8_length
from app.modules.proxy._service.support import (
    _PENDING_TOOL_CALL_ITEM_TYPES,
    _PENDING_TOOL_CALL_OUTPUT_ITEM_TYPE_BY_CALL_TYPE,
    _PENDING_TOOL_CALL_OUTPUT_ITEM_TYPES,
    _WebSocketRequestState,
)

logger = logging.getLogger("app.modules.proxy.service")
T = TypeVar("T")

_UPSTREAM_RESPONSE_CREATE_MAX_BYTES = UPSTREAM_RESPONSE_CREATE_MAX_BYTES
_UPSTREAM_RESPONSE_CREATE_WARN_BYTES = int(_UPSTREAM_RESPONSE_CREATE_MAX_BYTES * 0.8)
_OVERSIZED_RESPONSE_CREATE_LARGEST_ITEMS = 10
_RESPONSE_CREATE_HISTORY_OMISSION_NOTICE = (
    "[codex-lb omitted {count} historical input items to fit upstream websocket budget]"
)
_OVERSIZED_RESPONSE_CREATE_DUMP_DIR: Path | None = None
_RESPONSE_CREATE_DUMP_SUFFIX = ".response-create.json.gz"
_RESPONSE_CREATE_META_SUFFIX = ".meta.json"
_RESPONSE_CREATE_DUMP_SHA_SLUG_LEN = 16
_RESPONSE_CREATE_DUMP_MAX_PAIRS = 20
# Dumps run in a worker thread so hashing and gzip of a payload of up to the
# upstream response.create limit never stall the event loop. They are written
# one at a time, in submission order, so fingerprint dedup sees every earlier
# dump. Each pending dump pins its payload text until written, and ingress
# admits request bodies far above the upstream limit, so pending dumps are
# bounded by count and by the memory their payloads hold (the one being written
# included). A dump that would exceed either bound -- or a single payload larger
# than the byte budget -- is dropped with a warning naming the request and its
# size; nothing is hashed or encoded on the loop to report it.
_RESPONSE_CREATE_DUMP_MAX_PENDING = 4
_RESPONSE_CREATE_DUMP_MAX_PENDING_BYTES = 64 << 20
# The meta summary parses the whole payload and re-serializes its fields, which
# holds the GIL -- and so the event loop -- for the whole parse even in a worker
# thread. Above this size the summary is skipped; the payload file has it all.
_RESPONSE_CREATE_DUMP_SUMMARY_MAX_BYTES = 1 << 20
_response_create_dump_tasks: list[asyncio.Task[bool]] = []


@dataclass(slots=True)
class _PendingResponseCreateDump:
    request_id: str
    log_prefix: str
    # Memory the dump pins, released when its task finishes in any way.
    retained_bytes: int
    started: bool = False
    # Set when shutdown stops the dump before it started; it is logged then.
    abandoned: bool = False


@dataclass(frozen=True, slots=True)
class ResponseCreateDumpShutdown:
    """What shutdown did with the dumps still pending at its cutoff."""

    abandoned_request_ids: tuple[str, ...]
    writing_request_ids: tuple[str, ...]


_response_create_pending_dumps: dict[asyncio.Task[bool], _PendingResponseCreateDump] = {}


@dataclass(frozen=True, slots=True)
class _ResponseCreateDumpSource:
    """The request fields a dump records, detached from the live request state.

    A background dump holds only these, so the rest of the request state (replay
    buffers, futures, reservations) is not kept alive until the write finishes.
    """

    request_text: str | None
    request_id: str
    request_log_id: str | None
    response_id: str | None
    transport: str
    model: str | None
    reasoning_effort: str | None
    service_tier: str | None
    requested_service_tier: str | None
    actual_service_tier: str | None
    previous_response_id: str | None
    awaiting_response_created: bool
    replay_count: int

    @classmethod
    def of(cls, request_state: _WebSocketRequestState) -> _ResponseCreateDumpSource:
        return cls(
            request_text=request_state.request_text,
            request_id=request_state.request_id,
            request_log_id=request_state.request_log_id,
            response_id=request_state.response_id,
            transport=request_state.transport,
            model=request_state.model,
            reasoning_effort=request_state.reasoning_effort,
            service_tier=request_state.service_tier,
            requested_service_tier=request_state.requested_service_tier,
            actual_service_tier=request_state.actual_service_tier,
            previous_response_id=request_state.previous_response_id,
            awaiting_response_created=request_state.awaiting_response_created,
            replay_count=request_state.replay_count,
        )


_RESPONSE_CREATE_COMPATIBILITY_METADATA_HEADERS = (
    "x-codex-turn-metadata",
    "x-openai-subagent",
    "x-codex-parent-thread-id",
    "x-codex-window-id",
)


def _service_module() -> Any | None:
    return sys.modules.get("app.modules.proxy.service")


def _service_global_or(name: str, fallback: T) -> T:
    service_module = _service_module()
    if service_module is None:
        return fallback
    return cast(T, getattr(service_module, name, fallback))


def _upstream_response_create_max_bytes() -> int:
    return int(_service_global_or("_UPSTREAM_RESPONSE_CREATE_MAX_BYTES", _UPSTREAM_RESPONSE_CREATE_MAX_BYTES))


def _upstream_response_create_warn_bytes() -> int:
    fallback = int(_upstream_response_create_max_bytes() * 0.8)
    return int(_service_global_or("_UPSTREAM_RESPONSE_CREATE_WARN_BYTES", fallback))


def _oversized_response_create_largest_items() -> int:
    return int(
        _service_global_or(
            "_OVERSIZED_RESPONSE_CREATE_LARGEST_ITEMS",
            _OVERSIZED_RESPONSE_CREATE_LARGEST_ITEMS,
        )
    )


def _oversized_response_create_dump_dir() -> Path:
    configured_dir = _service_global_or("_OVERSIZED_RESPONSE_CREATE_DUMP_DIR", _OVERSIZED_RESPONSE_CREATE_DUMP_DIR)
    if configured_dir is not None:
        return configured_dir
    settings_factory = _service_global_or("get_settings", get_settings)
    data_dir = getattr(settings_factory(), "data_dir", DEFAULT_HOME_DIR)
    return data_dir / "debug" / "response-create-dumps"


def _response_create_dump_max_pairs() -> int:
    return int(_service_global_or("_RESPONSE_CREATE_DUMP_MAX_PAIRS", _RESPONSE_CREATE_DUMP_MAX_PAIRS))


def _existing_response_create_dump(dump_dir: Path, sha_slug: str) -> Path | None:
    """Return an existing *complete* dump for the same payload fingerprint.

    A dump only counts as existing when both the payload and its ``.meta.json``
    sibling are present. A lone payload file (e.g. a crash or disk-full failure
    between the two writes) is treated as absent so a retry can recreate the
    missing meta instead of permanently suppressing the capture operators are
    trying to diagnose.
    """
    try:
        matches = sorted(dump_dir.glob(f"*-{sha_slug}{_RESPONSE_CREATE_DUMP_SUFFIX}"))
    except OSError:
        return None
    for dump_path in matches:
        dump_id = dump_path.name[: -len(_RESPONSE_CREATE_DUMP_SUFFIX)]
        meta_path = dump_dir / f"{dump_id}{_RESPONSE_CREATE_META_SUFFIX}"
        if meta_path.exists():
            return dump_path
    return None


def _prune_response_create_dumps(dump_dir: Path, *, max_pairs: int) -> None:
    """Drop the oldest dump pairs so at most ``max_pairs`` remain.

    Dump ids are timestamp-prefixed, so lexicographic order is chronological.
    """
    try:
        dump_paths = sorted(dump_dir.glob(f"*{_RESPONSE_CREATE_DUMP_SUFFIX}"))
    except OSError:
        return
    excess = len(dump_paths) - max_pairs
    if excess <= 0:
        return
    for dump_path in dump_paths[:excess]:
        dump_id = dump_path.name[: -len(_RESPONSE_CREATE_DUMP_SUFFIX)]
        for stale_path in (dump_path, dump_dir / f"{dump_id}{_RESPONSE_CREATE_META_SUFFIX}"):
            try:
                stale_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Failed to prune response.create dump path=%s", stale_path, exc_info=True)


def _fingerprint_input_items(items: Sequence[JsonValue]) -> str:
    """Return stable SHA-256 fingerprint for input list canonical JSON.

    The digest covers the canonical dump of the whole list, but the dump is
    fed to the hash one item at a time: a list dump is ``[`` + the item dumps
    joined by ``,`` + ``]``, so the digest is unchanged while the temporary
    memory is bounded by the largest item instead of the whole input.
    """
    digest = sha256(b"[")
    for index, item in enumerate(items):
        if index:
            digest.update(b",")
        digest.update(json.dumps(item, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii"))
    digest.update(b"]")
    return digest.hexdigest()


def _input_context(input_items: Sequence[JsonValue], *, hoist: BaseHoist = NO_BASE_HOIST) -> tuple[int, str]:
    """Return the stored-context ``(count, fingerprint)`` of an ``input`` list.

    Stored context is measured on the base-equivalent input (developer messages
    as the old hoist left them), so every continuity decision sees the history
    it saw before developer messages stayed inline on the wire.
    """
    base_items = base_equivalent_input(list(input_items), hoist=hoist)
    return len(base_items), _fingerprint_input_items(base_items)


def _hoist_altered_developer_indices(items: list[JsonValue], *, hoist: BaseHoist) -> set[int]:
    """Wire indices of developer messages the old hoist would have moved or changed.

    That is every developer message the hoist removed, and one whose non-text
    parts it kept as a ``user`` item while moving its text.
    """
    base_items = dict(base_equivalent_input_positions(items, hoist=hoist))
    return {
        index
        for index, item in enumerate(items)
        if is_json_mapping(item) and item.get("role") == "developer" and base_items.get(index, None) != item
    }


def _input_developer_fingerprints(
    input_items: Sequence[JsonValue], *, hoist: BaseHoist = NO_BASE_HOIST
) -> tuple[str, ...]:
    """Fingerprints, in input order, of the developer messages the old hoist would have moved or changed."""
    items = list(input_items)
    altered = _hoist_altered_developer_indices(items, hoist=hoist)
    return tuple(_fingerprint_input_items([item]) for index, item in enumerate(items) if index in altered)


def _wire_remainder_after_stored_prefix(
    input_items: Sequence[JsonValue],
    stored_count: int,
    *,
    hoist: BaseHoist = NO_BASE_HOIST,
    stored_developer_fingerprints: tuple[str, ...] | None,
) -> list[JsonValue]:
    """Return what an anchored request sends after the anchor covers a stored prefix.

    The stored prefix is ``stored_count`` base-equivalent items; the range
    runs up to the next base-equivalent item after it. Its history items go
    (the anchor carries them). The developer messages the old hoist would
    have moved or changed are compared, in client order, with the stored
    turn's sequence (``stored_developer_fingerprints``): while each equals
    the recorded one at its position it goes. From the first that does not
    (new, changed, moved or extra), every developer message left in the
    range is sent in client order, including one the old hoist kept inline
    for JSON mode, so a refreshed or reordered message cannot end up behind
    a later one the anchor already carries. An unknown record (``None``)
    matches nothing. A sent developer message goes without its client item
    id: the anchored chain can already hold that id (a message the old hoist
    kept inline, or one with an attachment), and the hoist never sent the id
    of one it moved. Matching and the record use the client's items as sent.
    """
    items = list(input_items)
    if stored_count <= 0:
        return items
    positions = base_equivalent_input_positions(items, hoist=hoist)
    range_end = positions[stored_count][0] if stored_count < len(positions) else len(items)
    altered = _hoist_altered_developer_indices(items, hoist=hoist)
    recorded = stored_developer_fingerprints or ()
    matched = 0
    sent: list[JsonValue] = []
    for index, item in enumerate(items[:range_end]):
        if sent:
            if is_json_mapping(item) and item.get("role") == "developer" and item.get("type") in (None, "message"):
                sent.append(_without_item_id(item))
            continue
        if index not in altered:
            continue
        if matched < len(recorded) and _fingerprint_input_items([item]) == recorded[matched]:
            matched += 1
            continue
        sent.append(_without_item_id(item))
    return [*sent, *items[range_end:]]


def _without_item_id(item: JsonValue) -> JsonValue:
    if is_json_mapping(item) and "id" in item:
        return {key: value for key, value in item.items() if key != "id"}
    return item


def _input_prefix_matches_stored_context(
    input_value: JsonValue,
    *,
    stored_count: int,
    stored_fingerprint: str | None,
    hoist: BaseHoist = NO_BASE_HOIST,
) -> bool:
    """Whether ``input`` extends a stored context, judged on the base-equivalent input."""
    if stored_count <= 0 or stored_fingerprint is None:
        return False
    if not isinstance(input_value, list):
        return False
    base_items = base_equivalent_input(cast(list[JsonValue], input_value), hoist=hoist)
    if len(base_items) <= stored_count:
        return False
    return _fingerprint_input_items(base_items[:stored_count]) == stored_fingerprint


def _input_part_is_image(part: JsonValue) -> bool:
    return is_json_mapping(part) and part.get("type") == "input_image"


def _json_value_contains_input_image_part(value: JsonValue) -> bool:
    if _input_part_is_image(value):
        return True
    if isinstance(value, list):
        return any(_json_value_contains_input_image_part(item) for item in value)
    if is_json_mapping(value):
        return any(_json_value_contains_input_image_part(child) for child in value.values())
    return False


def _responses_request_contains_input_image(payload: ResponsesRequest) -> bool:
    """Return whether a Responses request carries any ``input_image`` part."""
    input_value = payload.input
    if not isinstance(input_value, list):
        return False
    return any(_json_value_contains_input_image_part(item) for item in input_value)


# Model-output items that call a tool or ask for approval. Several of them can
# come from one response, so they end the current turn only when they open a
# batch; their results stay in the same turn until the next model output.
_IMAGE_BYPASS_CALL_ITEM_TYPES = frozenset(
    {
        "function_call",
        "file_search_call",
        "custom_tool_call",
        "apply_patch_call",
        "tool_search_call",
        "web_search_call",
        "mcp_call",
        "mcp_approval_request",
        "local_shell_call",
        "image_generation_call",
        "computer_call",
        "code_interpreter_call",
    }
)


def _responses_request_requires_image_bridge_bypass(payload: ResponsesRequest) -> bool:
    """Keep new images and surviving external URLs off bridge pending slots."""
    input_value = payload.input
    if not isinstance(input_value, list):
        return False
    current_turn_has_image = False
    in_call_batch = False
    for item in input_value:
        raw_type = item.get("type") if is_json_mapping(item) else None
        item_type = raw_type if isinstance(raw_type, str) else None
        if is_json_mapping(item) and (item.get("role") == "assistant" or item_type == "reasoning"):
            current_turn_has_image = False
            in_call_batch = False
        elif item_type in _IMAGE_BYPASS_CALL_ITEM_TYPES:
            # A sibling call of the same batch keeps the results already seen.
            if not in_call_batch:
                current_turn_has_image = False
            in_call_batch = True
        else:
            is_result = item_type is not None and (
                item_type.endswith("_output") or item_type == "mcp_approval_response"
            )
            in_call_batch = in_call_batch and is_result
            if not current_turn_has_image:
                current_turn_has_image = _json_value_contains_input_image_part(item)
        if _json_value_contains_external_input_image(item):
            return True
    return current_turn_has_image


def _json_value_contains_external_input_image(value: JsonValue) -> bool:
    """Whether ``value`` holds an ``input_image`` part that still names an external URL.

    Recurses the same way :func:`_json_value_contains_input_image_part` does,
    rather than walking the two shapes ``_count_external_image_urls`` knows
    (a top-level item and its ``content`` array). The transport decision has to
    see every external URL the payload carries, including one nested in a
    ``function_call_output`` output array, because an image the URL inliner
    never visits is precisely the one that is still external when the request
    reaches the upstream websocket.
    """
    if _input_part_is_image(value):
        image_url = value.get("image_url") if is_json_mapping(value) else None
        # URL schemes are case-insensitive, and this decision is the fail-safe
        # direction: missing one sends a raw external URL to a websocket that
        # only accepts ``data:``. ``_count_external_image_urls`` still matches
        # case-sensitively; that is the bridge's own guard and out of scope here.
        return isinstance(image_url, str) and image_url.lower().startswith(("http://", "https://"))
    if isinstance(value, list):
        return any(_json_value_contains_external_input_image(item) for item in value)
    if is_json_mapping(value):
        return any(_json_value_contains_external_input_image(child) for child in value.values())
    return False


def _input_image_request_requires_http_upstream(
    payload: ResponsesRequest,
    *,
    payload_size_estimate_bytes: int,
) -> bool:
    """Return whether an ``input_image`` request must stay on the upstream HTTP transport.

    Inline ``data:`` images ride the upstream websocket unchanged, so carrying one
    is not by itself a reason to pin upstream HTTP; the bridge bypass exists to
    free bridge pending slots (#903), not to avoid the websocket. Two
    websocket-specific hazards survive, and only those keep the pin (#2363). A
    payload over the websocket frame budget would reach
    ``_prepare_websocket_response_create_payload``, which replaces every
    historical inline image with an omission notice. An external ``http(s)``
    image URL may still be there after ``_inline_content_images`` gives up on a
    failed fetch, and the upstream websocket does not accept one.

    The external-URL check recurses the whole input rather than reusing
    ``_count_external_image_urls``, whose traversal stops at an item's
    ``content`` array. An ``input_image`` nested deeper — in a
    ``function_call_output`` output array, a routine Codex tool-result shape —
    is exactly the one the URL inliner also never visits, so it is still
    external at the upstream and must keep the pin.
    """
    input_value = payload.input
    if not isinstance(input_value, list):
        return False
    if not any(_json_value_contains_input_image_part(item) for item in input_value):
        return False
    if payload_size_estimate_bytes > _ws_transport_payload_budget_bytes():
        return True
    return any(_json_value_contains_external_input_image(item) for item in input_value)


def _responses_request_uses_image_generation(payload: ResponsesRequest) -> bool:
    tools = payload.tools
    if not isinstance(tools, list):
        return False
    return any(is_json_mapping(tool) and tool.get("type") == "image_generation" for tool in tools)


def _response_create_text(
    payload: ResponsesRequest,
    *,
    include_type_field: bool,
    client_metadata: Mapping[str, JsonValue] | None,
) -> str:
    upstream_payload = dict(payload.to_payload())
    upstream_payload.pop("stream", None)
    upstream_payload.pop("background", None)
    if include_type_field:
        upstream_payload["type"] = "response.create"
    if client_metadata:
        upstream_payload["client_metadata"] = client_metadata
    _finalize_responses_lite_reasoning_context(
        upstream_payload,
        responses_lite=(
            _payload_uses_responses_lite(upstream_payload)
            or _payload_has_responses_lite_websocket_marker(upstream_payload)
        ),
    )
    return json.dumps(upstream_payload, ensure_ascii=True, separators=(",", ":"))


def _response_create_text_with_size_guard(
    payload: ResponsesRequest,
    *,
    include_type_field: bool,
    client_metadata: Mapping[str, JsonValue] | None,
    request_state: _WebSocketRequestState,
    transport: str,
) -> str | None:
    protected_agent_control_output_occurrences = (
        _historical_agent_control_output_occurrences(cast(list[JsonValue], payload.input))
        if isinstance(payload.input, list)
        else {}
    )
    upstream_payload = dict(payload.to_payload())
    upstream_payload.pop("stream", None)
    upstream_payload.pop("background", None)
    if include_type_field:
        upstream_payload["type"] = "response.create"
    if client_metadata:
        upstream_payload["client_metadata"] = client_metadata
    _finalize_responses_lite_reasoning_context(
        upstream_payload,
        responses_lite=(
            _payload_uses_responses_lite(upstream_payload)
            or _payload_has_responses_lite_websocket_marker(upstream_payload)
        ),
    )
    # Serialized in the pass that measures it; an oversized body is only
    # counted, then serialized once after slimming.
    max_bytes = _upstream_response_create_max_bytes()
    payload_size, text_data = compact_json_dumps_within(upstream_payload, max_bytes)
    if text_data is None:
        original_payload_size = payload_size
        slim_payload_for_upstream = _service_global_or(
            "_slim_response_create_payload_for_upstream",
            _slim_response_create_payload_for_upstream,
        )
        slimmed_payload, slim_summary = slim_payload_for_upstream(
            upstream_payload,
            max_bytes=max_bytes,
            protected_agent_control_output_occurrences=protected_agent_control_output_occurrences,
        )
        if slim_summary is not None:
            upstream_payload = slimmed_payload
            payload_size, text_data = compact_json_dumps_within(upstream_payload, max_bytes)
            logger.warning(
                (
                    "Slimmed response.create request_id=%s request_log_id=%s transport=%s "
                    "original_bytes=%s slimmed_bytes=%s "
                    "historical_tool_outputs_slimmed=%s historical_images_slimmed=%s"
                ),
                request_state.request_id,
                request_state.request_log_id,
                transport,
                original_payload_size,
                payload_size,
                slim_summary["historical_tool_outputs_slimmed"],
                slim_summary["historical_images_slimmed"],
            )
        if text_data is None:
            logger.warning(
                (
                    "Skipping oversized response.create retry body request_id=%s request_log_id=%s "
                    "transport=%s bytes=%s max_bytes=%s"
                ),
                request_state.request_id,
                request_state.request_log_id,
                transport,
                payload_size,
                max_bytes,
            )
            return None
    return text_data


def _response_create_text_with_account_installation_id(
    text_data: str,
    *,
    account: Any,
) -> str:
    installation_id = getattr(account, "codex_installation_id", None)
    if not isinstance(installation_id, str) or not installation_id.strip():
        installation_id = str(uuid4())
        try:
            setattr(account, "codex_installation_id", installation_id)
        except Exception:
            pass

    try:
        raw_payload = json.loads(text_data)
    except json.JSONDecodeError:
        return text_data
    if not isinstance(raw_payload, dict) or raw_payload.get("type") != "response.create":
        return text_data
    upstream_payload = cast(dict[str, JsonValue], raw_payload)

    apply_codex_installation_metadata(upstream_payload, installation_id)
    return json.dumps(upstream_payload, ensure_ascii=True, separators=(",", ":"))


def _slim_response_create_payload_for_upstream(
    payload: dict[str, JsonValue],
    *,
    max_bytes: int,
    protected_agent_control_output_occurrences: Mapping[tuple[str, str], tuple[bool, ...]] | None = None,
) -> tuple[dict[str, JsonValue], dict[str, int] | None]:
    input_value = payload.get("input")
    if not isinstance(input_value, list) or not input_value:
        return payload, None

    input_items = cast(list[JsonValue], deepcopy(input_value))
    preserve_from = _response_create_recent_suffix_start(input_items)
    historical = input_items[:preserve_from]
    recent = input_items[preserve_from:]

    tool_outputs_slimmed = 0
    images_slimmed = 0
    if protected_agent_control_output_occurrences is None:
        protected_agent_control_output_occurrences = _agent_control_tool_output_occurrences(historical)
    agent_control_output_counts: dict[tuple[str, str], int] = {}

    slimmed_historical: list[JsonValue] = []
    for item in historical:
        (
            slimmed_item,
            item_tool_outputs_slimmed,
            item_images_slimmed,
        ) = _slim_historical_response_input_item(
            item,
            protected_agent_control_output_occurrences=protected_agent_control_output_occurrences,
            agent_control_output_counts=agent_control_output_counts,
        )
        tool_outputs_slimmed += item_tool_outputs_slimmed
        images_slimmed += item_images_slimmed
        slimmed_historical.append(slimmed_item)

    candidate_payload = dict(payload)
    candidate_payload["input"] = slimmed_historical + recent

    if tool_outputs_slimmed == 0 and images_slimmed == 0:
        return payload, None

    return candidate_payload, {
        "historical_tool_outputs_slimmed": tool_outputs_slimmed,
        "historical_images_slimmed": images_slimmed,
    }


def _function_call_output_call_ids(input_items: list[JsonValue]) -> set[str]:
    call_ids: set[str] = set()
    for item in input_items:
        if not isinstance(item, dict) or item.get("type") not in _PENDING_TOOL_CALL_OUTPUT_ITEM_TYPES:
            continue
        call_id = item.get("call_id")
        if isinstance(call_id, str) and call_id:
            call_ids.add(call_id)
    return call_ids


def _missing_function_call_outputs_for_previous_response(
    input_items: list[JsonValue],
    *,
    pending_call_ids: list[str],
) -> list[str]:
    if not pending_call_ids:
        return []
    present_call_ids = _function_call_output_call_ids(input_items)
    return [call_id for call_id in pending_call_ids if call_id not in present_call_ids]


def _synthetic_interrupted_function_call_output(
    call_id: str,
    *,
    call_type: str = "function_call",
) -> dict[str, JsonValue]:
    output_type = _PENDING_TOOL_CALL_OUTPUT_ITEM_TYPE_BY_CALL_TYPE.get(call_type, "function_call_output")
    item: dict[str, JsonValue] = {
        "type": output_type,
        "call_id": call_id,
        "output": (
            "Tool call was not executed because the previous turn was interrupted before tool output was available."
        ),
    }
    if output_type == "apply_patch_call_output":
        item["status"] = "failed"
    return item


def _inject_missing_interrupted_function_call_outputs(
    input_items: list[JsonValue],
    *,
    missing_call_ids: list[str],
    pending_call_types: Mapping[str, str] | None = None,
) -> list[JsonValue]:
    if not missing_call_ids:
        return input_items
    call_types = pending_call_types or {}
    return [
        *[
            _synthetic_interrupted_function_call_output(
                call_id,
                call_type=call_types.get(call_id, "function_call"),
            )
            for call_id in missing_call_ids
        ],
        *input_items,
    ]


def _response_output_item_done_tool_call(payload: dict[str, JsonValue] | None) -> tuple[str, str] | None:
    """Return ``(call_id, item_type)`` for a completed tool-call output item.

    Covers every tool-call item type that upstream requires a matching output
    item for on the next anchored request: ``function_call``,
    ``custom_tool_call``, and ``apply_patch_call``.
    """
    if not isinstance(payload, dict) or payload.get("type") != "response.output_item.done":
        return None
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if not isinstance(item_type, str) or item_type not in _PENDING_TOOL_CALL_ITEM_TYPES:
        return None
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    return call_id, item_type


def _slim_historical_response_input_item(
    item: JsonValue,
    *,
    protected_agent_control_output_occurrences: Mapping[tuple[str, str], tuple[bool, ...]],
    agent_control_output_counts: dict[tuple[str, str], int],
) -> tuple[JsonValue, int, int]:
    if not is_json_mapping(item):
        return item, 0, 0

    item_mapping = dict(cast(dict[str, JsonValue], deepcopy(item)))
    tool_outputs_slimmed = 0
    images_slimmed = 0

    item_type = item_mapping.get("type")
    if isinstance(item_type, str) and item_type in _AGENT_CONTROL_OUTPUT_ITEM_TYPES:
        call_id = item_mapping.get("call_id")
        if isinstance(call_id, str) and call_id:
            key = (item_type, call_id)
            occurrence = agent_control_output_counts.get(key, 0)
            agent_control_output_counts[key] = occurrence + 1
            namespaced_flags = protected_agent_control_output_occurrences.get(key, ())
            if occurrence < len(namespaced_flags) and namespaced_flags[occurrence]:
                return item_mapping, tool_outputs_slimmed, images_slimmed
    if isinstance(item_type, str) and item_type in _PENDING_TOOL_CALL_OUTPUT_ITEM_TYPES:
        output = item_mapping.get("output")
        if isinstance(output, str):
            if _should_slim_historical_tool_output(output):
                item_mapping["output"] = _RESPONSE_CREATE_TOOL_OUTPUT_OMISSION_NOTICE.format(
                    bytes=len(output.encode("utf-8"))
                )
                tool_outputs_slimmed += 1
        else:
            slimmed_output, output_images_slimmed = _slim_historical_response_content(output)
            if output_images_slimmed > 0:
                item_mapping["output"] = slimmed_output
                images_slimmed += output_images_slimmed

    content = item_mapping.get("content")
    slimmed_content, content_images_slimmed = _slim_historical_response_content(content)
    if content_images_slimmed > 0:
        item_mapping["content"] = slimmed_content
        images_slimmed += content_images_slimmed

    if item_type == "input_image" and _is_inline_image_reference(item_mapping.get("image_url")):
        return _response_create_inline_image_notice_item(), tool_outputs_slimmed, images_slimmed + 1

    return item_mapping, tool_outputs_slimmed, images_slimmed


def _response_create_history_omission_notice_item(count: int) -> dict[str, JsonValue]:
    return {
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": _RESPONSE_CREATE_HISTORY_OMISSION_NOTICE.format(count=count),
            }
        ],
    }


async def _inline_top_level_input_image_urls(
    payload: dict[str, JsonValue],
    session: ImageFetchSession,
    connect_timeout: float,
) -> dict[str, JsonValue]:
    input_value = payload.get("input")
    if not isinstance(input_value, list):
        return payload

    updated_input: list[JsonValue] = []
    changed = False
    for item in input_value:
        if not isinstance(item, dict) or item.get("type") != "input_image":
            updated_input.append(item)
            continue
        inline_content_images = _service_global_or("_inline_content_images", _inline_content_images)
        updated_item, item_changed = await inline_content_images(item, session, connect_timeout)
        updated_input.append(updated_item)
        changed = changed or item_changed
    if not changed:
        return payload

    updated_payload = dict(payload)
    updated_payload["input"] = updated_input
    return updated_payload


def _count_external_image_urls(payload: dict[str, JsonValue]) -> int:
    """Count input_image items that still reference an external (non data:) URL."""
    input_value = payload.get("input")
    if not isinstance(input_value, list):
        return 0
    count = 0
    for item in input_value:
        if not isinstance(item, dict):
            continue
        content_value = item.get("content")
        content_parts = content_value if isinstance(content_value, list) else [content_value]
        if item.get("type") == "input_image":
            content_parts = [item, *content_parts]
        for part in content_parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "input_image":
                continue
            image_url = part.get("image_url")
            if isinstance(image_url, str) and image_url.startswith(("http://", "https://")):
                count += 1
    return count


def _enforce_response_create_size_limit(request_state: _WebSocketRequestState) -> None:
    request_text = request_state.request_text
    if not request_text:
        return

    payload_size = utf8_length(request_text)
    warn_bytes = _upstream_response_create_warn_bytes()
    max_bytes = _upstream_response_create_max_bytes()
    if payload_size > warn_bytes:
        # A native Codex history goes upstream complete on every turn, so a
        # long thread crosses this size on each request; the limit below still
        # rejects an oversized frame.
        logger.log(
            logging.DEBUG if request_state.native_codex_full_history else logging.WARNING,
            (
                "Large response.create prepared request_id=%s request_log_id=%s "
                "transport=%s bytes=%s previous_response_id=%s"
            ),
            request_state.request_id,
            request_state.request_log_id,
            request_state.transport,
            payload_size,
            request_state.previous_response_id,
        )
    if payload_size <= max_bytes:
        return

    payload = _response_create_too_large_error_envelope(payload_size, max_bytes)
    error = payload["error"]
    _submit_response_create_dump(
        request_state,
        account_id_value=None,
        error_code=cast(str, error.get("code") or "payload_too_large"),
        error_message=error.get("message"),
        log_prefix="guarded",
    )
    # 400, not 413: the Codex client surfaces 400 immediately as a non-retryable
    # invalid request, while 413 burns five full-payload retries and then pins the
    # session to HTTP transport.
    raise ProxyResponseError(
        400,
        payload,
        failure_phase="validation",
        failure_detail=f"response.create_bytes={payload_size}",
    )


def _maybe_dump_oversized_response_create_request(
    request_state: _WebSocketRequestState,
    *,
    account_id_value: str | None,
    error_code: str,
    error_message: str | None,
) -> None:
    should_dump_oversized_response_create = _service_global_or(
        "_should_dump_oversized_response_create",
        _should_dump_oversized_response_create,
    )
    if not should_dump_oversized_response_create(error_code, error_message):
        return
    _submit_response_create_dump(
        request_state,
        account_id_value=account_id_value,
        error_code=error_code,
        error_message=error_message,
        log_prefix="oversized",
    )


def _submit_response_create_dump(
    request_state: _WebSocketRequestState,
    *,
    account_id_value: str | None,
    error_code: str,
    error_message: str | None,
    log_prefix: str,
    scheduler: Scheduler = REAL_SCHEDULER,
) -> None:
    """Write a response.create dump without blocking the running event loop.

    Hashing and gzip-compressing a payload near the upstream size limit takes
    seconds of CPU, so on a running loop the write is queued behind earlier
    dumps and runs in a worker thread with a snapshot of the recorded request
    fields. Without a running loop (synchronous callers) it is written inline.
    """
    write_response_create_dump = _service_global_or("_write_response_create_dump", _write_response_create_dump)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        write_response_create_dump(
            request_state,
            account_id_value=account_id_value,
            error_code=error_code,
            error_message=error_message,
            log_prefix=log_prefix,
        )
        return
    # A dump queued on a loop that has since closed can never finish.
    for stale in [task for task in _response_create_dump_tasks if task.get_loop() is not loop]:
        _forget_response_create_dump(stale)
    # O(1): the size of the str object this dump would keep alive.
    retained_bytes = sys.getsizeof(request_state.request_text or "")
    pending_bytes = sum(pending.retained_bytes for pending in _response_create_pending_dumps.values())
    if (
        len(_response_create_dump_tasks) >= _RESPONSE_CREATE_DUMP_MAX_PENDING
        or pending_bytes + retained_bytes > _RESPONSE_CREATE_DUMP_MAX_PENDING_BYTES
    ):
        logger.warning(
            (
                "Dropped %s response.create dump request_id=%s request_log_id=%s retained_bytes=%s: "
                "%s dumps pending holding %s bytes, limits %s dumps and %s bytes"
            ),
            log_prefix,
            request_state.request_id,
            request_state.request_log_id,
            retained_bytes,
            len(_response_create_dump_tasks),
            pending_bytes,
            _RESPONSE_CREATE_DUMP_MAX_PENDING,
            _RESPONSE_CREATE_DUMP_MAX_PENDING_BYTES,
        )
        return
    previous = _response_create_dump_tasks[-1] if _response_create_dump_tasks else None
    task = scheduler.create_task(
        _write_response_create_dump_after(
            previous,
            write_response_create_dump,
            _ResponseCreateDumpSource.of(request_state),
            account_id_value=account_id_value,
            error_code=error_code,
            error_message=error_message,
            log_prefix=log_prefix,
        )
    )
    _response_create_dump_tasks.append(task)
    _response_create_pending_dumps[task] = _PendingResponseCreateDump(
        request_id=request_state.request_id,
        log_prefix=log_prefix,
        retained_bytes=retained_bytes,
    )
    task.add_done_callback(_finish_response_create_dump)


async def _write_response_create_dump_after(
    previous: asyncio.Task[bool] | None,
    write_response_create_dump: Any,
    source: _ResponseCreateDumpSource,
    **kwargs: Any,
) -> bool:
    if previous is not None:
        # Whatever the earlier dump's outcome, this one starts after it.
        await asyncio.wait((previous,))
    current = asyncio.current_task()
    pending = _response_create_pending_dumps.get(current) if current is not None else None
    if pending is not None:
        pending.started = True
    return bool(await asyncio.to_thread(write_response_create_dump, source, **kwargs))


async def drain_response_create_dumps(
    timeout_seconds: float, *, scheduler: Scheduler = REAL_SCHEDULER
) -> ResponseCreateDumpShutdown:
    """Let pending dumps finish within ``timeout_seconds``, then stop the queued ones.

    At the cutoff every dump that has not started writing is cancelled, so it
    can no longer write, and logged with its request id. A dump whose write has
    started cannot be stopped (it runs in a worker thread); it is reported so
    the caller can say it may be interrupted if the process exits first.
    """
    pending = list(_response_create_dump_tasks)
    if pending and timeout_seconds > 0:
        await scheduler.wait(set(pending), timeout=timeout_seconds)
    abandoned: list[str] = []
    writing: list[str] = []
    for task in pending:
        record = _response_create_pending_dumps.get(task)
        if task.done() or record is None:
            continue
        if record.started:
            writing.append(record.request_id)
            continue
        record.abandoned = True
        task.cancel()
        abandoned.append(record.request_id)
        logger.warning(
            "Dropped %s response.create dump request_id=%s: not written before shutdown",
            record.log_prefix,
            record.request_id,
        )
    return ResponseCreateDumpShutdown(tuple(abandoned), tuple(writing))


def _forget_response_create_dump(task: asyncio.Task[bool]) -> _PendingResponseCreateDump | None:
    if task in _response_create_dump_tasks:
        _response_create_dump_tasks.remove(task)
    return _response_create_pending_dumps.pop(task, None)


def _finish_response_create_dump(task: asyncio.Task[bool]) -> None:
    record = _forget_response_create_dump(task)
    request_id = record.request_id if record is not None else None
    log_prefix = record.log_prefix if record is not None else "queued"
    if task.cancelled():
        if record is None or not (record.started or record.abandoned):
            logger.warning(
                "Dropped %s response.create dump request_id=%s: cancelled before it was written",
                log_prefix,
                request_id,
            )
        # A started write keeps running in its worker thread; an abandoned one
        # was logged when shutdown stopped it.
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "Failed to write %s response.create dump request_id=%s",
            log_prefix,
            request_id,
            exc_info=exc,
        )


def _write_response_create_dump(
    request_state: _WebSocketRequestState | _ResponseCreateDumpSource,
    *,
    account_id_value: str | None,
    error_code: str,
    error_message: str | None,
    log_prefix: str,
) -> bool:
    request_text = request_state.request_text
    if not request_text:
        return False

    payload_bytes = request_text.encode("utf-8")
    request_sha = sha256(payload_bytes).hexdigest()
    sha_slug = request_sha[:_RESPONSE_CREATE_DUMP_SHA_SLUG_LEN]
    dump_dir = _oversized_response_create_dump_dir()

    existing_dump_path = _existing_response_create_dump(dump_dir, sha_slug)
    if existing_dump_path is not None:
        logger.warning(
            (
                "Skipped duplicate %s response.create dump request_id=%s request_log_id=%s "
                "request_text_sha256=%s existing_dump_path=%s bytes=%s"
            ),
            log_prefix,
            request_state.request_id,
            request_state.request_log_id,
            request_sha,
            existing_dump_path,
            len(payload_bytes),
        )
        return False

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    dump_id = "-".join(
        (
            timestamp,
            _safe_dump_slug(request_state.transport, fallback="transport"),
            _safe_dump_slug(request_state.model, fallback="model"),
            _safe_dump_slug(
                request_state.request_log_id or request_state.response_id or request_state.request_id,
                fallback="request",
            ),
            sha_slug,
        )
    )
    dump_path = dump_dir / f"{dump_id}{_RESPONSE_CREATE_DUMP_SUFFIX}"
    meta_path = dump_dir / f"{dump_id}{_RESPONSE_CREATE_META_SUFFIX}"

    meta: dict[str, JsonValue] = {
        "dump_id": dump_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "reason": {
            "error_code": error_code,
            "error_message": error_message,
        },
        "request": {
            "account_id": account_id_value,
            "request_id": request_state.request_id,
            "request_log_id": request_state.request_log_id,
            "response_id": request_state.response_id,
            "transport": request_state.transport,
            "model": request_state.model,
            "reasoning_effort": request_state.reasoning_effort,
            "service_tier": request_state.service_tier,
            "requested_service_tier": request_state.requested_service_tier,
            "actual_service_tier": request_state.actual_service_tier,
            "previous_response_id": request_state.previous_response_id,
            "awaiting_response_created": request_state.awaiting_response_created,
            "replay_count": request_state.replay_count,
            "request_text_bytes": len(payload_bytes),
            "request_text_chars": len(request_text),
            "request_text_sha256": request_sha,
        },
        "paths": {
            "dump_path": str(dump_path),
            "meta_path": str(meta_path),
        },
    }

    summary_max_bytes = int(
        _service_global_or("_RESPONSE_CREATE_DUMP_SUMMARY_MAX_BYTES", _RESPONSE_CREATE_DUMP_SUMMARY_MAX_BYTES)
    )
    if len(payload_bytes) > summary_max_bytes:
        meta["summary"] = {"skipped": "payload larger than the summary limit", "limit_bytes": summary_max_bytes}
    else:
        try:
            parsed_payload = json.loads(request_text)
        except json.JSONDecodeError as exc:
            meta["parse_error"] = str(exc)
        else:
            meta["summary"] = _summarize_parsed_response_create_payload(parsed_payload)

    max_pairs = _response_create_dump_max_pairs()
    # Trim any pre-existing over-cap backlog before the new write. If the volume
    # is already full, the write below raises and returns without pruning, so a
    # directory left above the bound (e.g. after an upgrade that lowers the cap)
    # would otherwise stay full on the exact failure the cap is meant to bound.
    _prune_response_create_dumps(dump_dir, max_pairs=max_pairs)

    try:
        dump_dir.mkdir(parents=True, exist_ok=True)
        with gzip.open(dump_path, "wt", encoding="utf-8") as handle:
            handle.write(request_text)
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        logger.exception(
            "Failed to dump %s response.create payload request_id=%s request_log_id=%s",
            log_prefix,
            request_state.request_id,
            request_state.request_log_id,
        )
        return False

    _prune_response_create_dumps(dump_dir, max_pairs=max_pairs)

    logger.warning(
        "Saved %s response.create dump request_id=%s request_log_id=%s dump_path=%s meta_path=%s bytes=%s",
        log_prefix,
        request_state.request_id,
        request_state.request_log_id,
        dump_path,
        meta_path,
        len(payload_bytes),
    )
    return True


def _summarize_parsed_response_create_payload(parsed_payload: object) -> JsonValue:
    if isinstance(parsed_payload, dict):
        return _summarize_response_create_payload(cast(dict[str, JsonValue], parsed_payload))
    return {"payload_type": type(parsed_payload).__name__}


def _should_dump_oversized_response_create(error_code: str, error_message: str | None) -> bool:
    if error_code != "stream_incomplete" or not error_message:
        return False
    normalized = error_message.lower()
    return "1009" in normalized or "message too big" in normalized


def _safe_dump_slug(value: str | None, *, fallback: str) -> str:
    if not value:
        return fallback
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    if not normalized:
        return fallback
    return normalized[:80]


def _summarize_response_create_payload(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    field_sizes = sorted(
        (
            {
                "key": key,
                "size_bytes": _json_size_bytes(value),
            }
            for key, value in payload.items()
        ),
        key=lambda item: int(item["size_bytes"]),
        reverse=True,
    )
    summary: dict[str, JsonValue] = {
        "top_level_keys": list(payload.keys()),
        "top_level_field_sizes": cast(JsonValue, field_sizes),
    }
    input_summary = _summarize_response_create_input(payload.get("input"))
    if input_summary is not None:
        summary["input"] = input_summary
    return summary


def _summarize_response_create_input(input_value: JsonValue) -> dict[str, JsonValue] | None:
    if not isinstance(input_value, list):
        return None

    input_items = cast(list[JsonValue], input_value)
    role_counts: dict[str, int] = {}
    item_type_counts: dict[str, int] = {}
    content_part_type_counts: dict[str, int] = {}
    largest_items: list[dict[str, JsonValue]] = []

    for index, item in enumerate(input_items):
        item_summary: dict[str, JsonValue] = {
            "index": index,
            "size_bytes": _json_size_bytes(item),
        }
        if isinstance(item, dict):
            item_object = item
            role = item_object.get("role")
            if isinstance(role, str):
                item_summary["role"] = role
                role_counts[role] = role_counts.get(role, 0) + 1
            item_type = item_object.get("type")
            if isinstance(item_type, str):
                item_summary["type"] = item_type
                item_type_counts[item_type] = item_type_counts.get(item_type, 0) + 1
            content = item_object.get("content")
            if isinstance(content, list):
                item_summary["content_parts"] = len(content)
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    part_object = part
                    part_type = part_object.get("type")
                    if isinstance(part_type, str):
                        content_part_type_counts[part_type] = content_part_type_counts.get(part_type, 0) + 1
        largest_items.append(item_summary)

    largest_items.sort(key=lambda item: cast(int, item["size_bytes"]), reverse=True)
    summary: dict[str, JsonValue] = {
        "count": len(input_value),
        "role_counts": cast(JsonValue, role_counts),
        "item_type_counts": cast(JsonValue, item_type_counts),
        "content_part_type_counts": cast(JsonValue, content_part_type_counts),
        "largest_items": cast(JsonValue, largest_items[: _oversized_response_create_largest_items()]),
    }
    return summary


def _json_size_bytes(value: JsonValue) -> int:
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))


def _response_create_client_metadata(
    payload: Mapping[str, JsonValue],
    *,
    headers: Mapping[str, str],
    codex_installation_id: str | None = None,
    preserve_existing_responses_lite: bool = False,
) -> Mapping[str, JsonValue] | None:
    raw_value = payload.get("client_metadata")
    client_metadata: dict[str, JsonValue] = {}
    if is_json_mapping(raw_value):
        for key, value in raw_value.items():
            if isinstance(key, str) and key.lower() != CODEX_INSTALLATION_ID_HEADER:
                client_metadata[key] = value

    normalized_headers = {key.lower(): value for key, value in headers.items()}
    for header_name in _RESPONSE_CREATE_COMPATIBILITY_METADATA_HEADERS:
        metadata_value = normalized_headers.get(header_name)
        if isinstance(metadata_value, str) and metadata_value.strip():
            client_metadata.setdefault(header_name, metadata_value)

    metadata_container: dict[str, JsonValue] = {"client_metadata": client_metadata}
    apply_codex_installation_metadata(metadata_container, codex_installation_id)
    normalized_metadata = metadata_container.get("client_metadata")
    client_metadata = dict(normalized_metadata) if is_json_mapping(normalized_metadata) else {}
    client_metadata = _normalize_responses_lite_websocket_client_metadata(
        payload,
        client_metadata,
        preserve_existing=preserve_existing_responses_lite,
    )
    return client_metadata or None
