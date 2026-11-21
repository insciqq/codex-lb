"""Peak memory of large ``response.create`` bodies on the request path.

A body of tens of megabytes used to be held as several full copies at once
(the parsed payload, its JSON dump, the dump's encoding, a base64 copy for the
native egress helper and that copy's own dump), so a single image-heavy turn
could exhaust the process. These tests run a synthetic ~50 MB body through
the preparation path under ``tracemalloc`` and bound the peak to a small
multiple of the body size.
"""

from __future__ import annotations

import base64
import gc
import json
import os
import stat
import tracemalloc
from collections.abc import Callable, Generator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.core.utils.payload_size as payload_size_module
import app.modules.proxy._service.http_bridge.request_submit as request_submit_module
import app.modules.proxy._service.response_create as response_create_module
from app.core.clients import native_egress as native_egress_module
from app.core.clients import proxy as proxy_module
from app.core.clients.native_egress import SubprocessNativeEgressClient
from app.core.openai.requests import ResponsesRequest
from app.core.types import JsonValue
from app.core.utils.payload_size import compact_json_length, utf8_length
from app.modules.proxy import service as proxy_service
from app.modules.proxy._service.response_create import (
    _fingerprint_input_items,
    _response_create_text_with_size_guard,
)

pytestmark = pytest.mark.unit

_BODY_BYTES = 48 * 1024 * 1024
_IMAGE_COUNT = 8


def _image_heavy_payload(total_bytes: int) -> dict[str, JsonValue]:
    """A Codex-shaped thread whose history carries large inline screenshots."""
    image_chars = total_bytes // _IMAGE_COUNT
    image = "data:image/png;base64," + base64.b64encode(os.urandom(image_chars * 3 // 4)).decode("ascii")
    items: list[JsonValue] = [{"role": "user", "content": [{"type": "input_text", "text": "look at these"}]}]
    for index in range(_IMAGE_COUNT):
        items.append({"type": "function_call", "call_id": f"call_{index}", "name": "view_image", "arguments": "{}"})
        items.append({"type": "function_call_output", "call_id": f"call_{index}", "output": "ok"})
        items.append({"role": "user", "content": [{"type": "input_image", "image_url": image}]})
        items.extend({"role": "assistant", "content": [{"type": "output_text", "text": "noted"}]} for _ in range(10))
    items.append({"role": "user", "content": [{"type": "input_text", "text": "and now?"}]})
    return {"model": "gpt-5.4", "instructions": "be brief", "input": items, "stream": True}


@contextmanager
def _traced_peak() -> Generator[Callable[[], int]]:
    """Yield a reader for the peak traced allocation since entry, in bytes."""
    gc.collect()
    tracemalloc.start()
    baseline, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    peak = 0

    def read() -> int:
        return peak

    try:
        yield read
    finally:
        _, traced_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peak = traced_peak - baseline


_SILENT_HELPER_SOURCE = r"""#!/usr/bin/env python3
import base64
import json
import re
import sys

stdin = sys.stdin.buffer


def lines():
    # Yield a bounded prefix and the length of every command line without
    # holding a whole request body in this process.
    prefix, length, pending = b"", 0, b""
    while True:
        chunk = pending or stdin.read1(1 << 20)
        pending = b""
        if not chunk:
            return
        newline = chunk.find(b"\n")
        part = chunk if newline < 0 else chunk[:newline]
        if len(prefix) < 4096:
            prefix += part[: 4096 - len(prefix)]
        length += len(part)
        if newline >= 0:
            yield prefix, length
            prefix, length, pending = b"", 0, chunk[newline + 1 :]


def emit(event):
    print(json.dumps(event), flush=True)


commands = lines()
next(commands)
emit({
    "type": "server_hello",
    "protocol_version": 1,
    "capabilities": [
        "failure_provenance_v1", "http", "http2_profile_v1", "http_compact_collect_v1",
        "http_compact_sse_v1", "http_sse_v1", "http_responses_events_v1",
        "http_responses_completion_v1", "websocket", "websocket_responses_events_v1",
        "websocket_responses_routing_v1", "websocket_send_ack",
    ],
})
for prefix, length in commands:
    command_type = re.search(rb'"type":"([^"]+)"', prefix).group(1).decode()
    request_id = re.search(rb'"request_id":"([^"]+)"', prefix).group(1).decode()
    if command_type == "cancel":
        emit({"type": "cancelled", "request_id": request_id})
        continue
    emit({
        "type": "head", "request_id": request_id, "status": 400, "http_version": "HTTP/2.0",
        "headers": [["content-type", "application/json"]],
    })
    error = {"error": {"message": f"line_bytes={length}", "type": "invalid_request_error", "code": "probe"}}
    emit({"type": "chunk", "request_id": request_id, "data": base64.b64encode(json.dumps(error).encode()).decode()})
    emit({"type": "end", "request_id": request_id})
"""


class _UnusedAiohttpSession:
    def post(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("the native egress helper must carry the request")


@pytest.mark.asyncio
async def test_large_direct_http_dispatch_peaks_at_a_small_multiple_of_the_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Settings:
        upstream_base_url = "https://chatgpt.com/backend-api"
        upstream_connect_timeout_seconds = 8.0
        stream_idle_timeout_seconds = 45.0
        trace_channels = frozenset()
        proxy_request_budget_seconds = 60.0

    monkeypatch.setattr(proxy_module, "get_settings", lambda: Settings())
    monkeypatch.setattr(proxy_module, "_maybe_log_upstream_request_start", lambda **kwargs: None)
    monkeypatch.setattr(proxy_module, "_maybe_log_upstream_request_complete", lambda **kwargs: None)
    helper = tmp_path / "native-helper"
    helper.write_text(_SILENT_HELPER_SOURCE, encoding="utf-8")
    helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
    client = SubprocessNativeEgressClient(helper)
    payload = ResponsesRequest.model_validate(_image_heavy_payload(_BODY_BYTES))
    body_bytes = compact_json_length(payload.to_payload())

    try:
        with _traced_peak() as peak:
            events = [
                event
                async for event in proxy_module.stream_responses(
                    payload,
                    headers={"originator": "codex_cli_rs", "user-agent": "codex_cli_rs/0.150.0"},
                    access_token="token",
                    account_id="acc_1",
                    session=cast(Any, _UnusedAiohttpSession()),
                    native_egress_client=client,
                )
            ]
    finally:
        await client.aclose()

    # Over the websocket frame budget, so the body went to upstream HTTP in
    # full: base64 inflates it by 4/3 inside the helper command line.
    (probe,) = [event for event in events if "line_bytes=" in event]
    line_bytes = int(probe.split("line_bytes=", 1)[1].split('"', 1)[0])
    assert line_bytes > body_bytes * 4 // 3
    # The JSON text and its encoding while the body is serialized, with
    # headroom; holding the JSON text for the whole attempt and building the
    # command line as base64 bytes, a str, a dump, its encoding and the pipe
    # buffer of the whole line peaked at about six times the body.
    assert peak() < 2.5 * body_bytes


@pytest.mark.asyncio
async def test_oversized_websocket_response_create_is_slimmed_without_full_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.test_proxy_utils import _make_proxy_settings, _repo_factory, _RequestLogsRecorder, _SettingsCache

    settings = _make_proxy_settings()
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _SettingsCache(settings))
    service = proxy_service.ProxyService(_repo_factory(_RequestLogsRecorder()))
    monkeypatch.setattr(service, "_refresh_websocket_api_key_policy", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_reserve_websocket_api_key_usage", AsyncMock(return_value=None))
    frame = {"type": "response.create", **_image_heavy_payload(_BODY_BYTES)}
    body_bytes = compact_json_length(frame)

    with _traced_peak() as peak:
        prepared = await service._prepare_websocket_response_create_request(
            frame,
            headers={},
            codex_session_affinity=True,
            openai_cache_affinity=True,
            sticky_threads_enabled=False,
            openai_cache_affinity_max_age_seconds=0,
            api_key=None,
        )

    # Historical screenshots were replaced by omission notices.
    assert len(prepared.text_data) < proxy_module.UPSTREAM_RESPONSE_CREATE_MAX_BYTES
    assert json.loads(prepared.text_data)["input"][-1]["content"][0]["text"] == "and now?"
    # Sizing encodes the body one input item at a time and keeps the pieces
    # only until they pass the websocket budget, then only the slimmed frame is
    # serialized: the peak is the budget plus the item that crosses it (which
    # older encoders build twice), not a multiple of the body. A full dump plus
    # its encoding (and a whole-input fingerprint dump) peaked at twice the body.
    largest_item = max(compact_json_length(item) for item in cast(list[JsonValue], frame["input"]))
    assert peak() < proxy_module.UPSTREAM_RESPONSE_CREATE_MAX_BYTES + 2 * largest_item + 1024 * 1024
    assert peak() < body_bytes


class _CountingEncoder:
    def __init__(self, encoder: json.JSONEncoder) -> None:
        self._encoder = encoder
        self.encoded_chars = 0

    def encode(self, value: object) -> str:
        text = self._encoder.encode(value)
        self.encoded_chars += len(text)
        return text


@pytest.mark.parametrize("path", ["bridge", "size-guard"])
def test_routine_response_create_is_serialized_in_the_pass_that_measures_it(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    encoder = _CountingEncoder(payload_size_module._COMPACT_ENCODER)
    monkeypatch.setattr(payload_size_module, "_COMPACT_ENCODER", encoder)
    module_json = {module: MagicMock(wraps=json) for module in (request_submit_module, response_create_module)}
    for module, spy in module_json.items():
        monkeypatch.setattr(module, "json", spy)
    payload = ResponsesRequest.model_validate(_image_heavy_payload(2 * 1024 * 1024))

    if path == "bridge":
        service = proxy_service.ProxyService(cast(Any, None))
        _, text = service._prepare_http_bridge_request(payload, {}, api_key=None, api_key_reservation=None)
    else:
        request_state = proxy_service._WebSocketRequestState(
            request_id="req_size_guard",
            model=payload.model,
            service_tier=None,
            reasoning_effort=None,
            api_key_reservation=None,
            started_at=0.0,
        )
        guarded = _response_create_text_with_size_guard(
            payload,
            include_type_field=True,
            client_metadata=None,
            request_state=request_state,
            transport="websocket",
        )
        assert guarded is not None
        text = guarded

    assert json.loads(text)["input"] == payload.to_payload()["input"]
    # Measuring the body encoded it once, and that encoding became the frame:
    # no second dump of the whole body.
    assert encoder.encoded_chars <= len(text)
    whole_body_dumps = [
        call
        for spy in module_json.values()
        for call in spy.dumps.call_args_list
        if isinstance(call.args[0], dict) and "input" in call.args[0] and "model" in call.args[0]
    ]
    assert whole_body_dumps == []


def test_input_fingerprint_matches_the_whole_list_digest_with_bounded_memory() -> None:
    items: list[JsonValue] = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "héllo 한국어 \ud83d"}]},
        {"z": 1, "a": [1.5, None, True, {"nested": "x"}]},
        "plain",
        [],
    ]
    canonical = json.dumps(items, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    assert _fingerprint_input_items(items) == sha256(canonical.encode("utf-8")).hexdigest()
    assert _fingerprint_input_items([]) == sha256(b"[]").hexdigest()

    large = cast(list[JsonValue], _image_heavy_payload(16 * 1024 * 1024)["input"])
    total = compact_json_length(large)
    with _traced_peak() as peak:
        _fingerprint_input_items(large)
    assert peak() < total // 2


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        {"a": []},
        {"a": [1, "x", None, {"b": [True, 1.25, float("nan")]}], "c": "dé\ud83d", "e": {}},
        {"input": [{"type": "input_text", "text": "x" * 10}], "tools": [], "n": -3},
        {1: "non-string key", "a": [1]},
        [{"a": 1}, "b"],
        "text ☃",
        12.5,
        None,
    ],
)
def test_compact_json_length_matches_the_compact_dump(value: object) -> None:
    assert compact_json_length(value) == len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


@pytest.mark.parametrize(
    "text",
    ["", "ascii only", "é", "한국어 \U0001f600", "\ud83d lone surrogate", "x" * 3_000_000 + "é" + "y" * 10],
)
def test_utf8_length_matches_the_encoding(text: str) -> None:
    assert utf8_length(text) == len(text.encode("utf-8", "surrogatepass"))


@pytest.mark.parametrize("body_bytes", [0, 1, 2, 3, 3 * 256 * 1024, 2 * 3 * 256 * 1024 + 1])
def test_native_command_line_carries_the_whole_body(body_bytes: int) -> None:
    body = os.urandom(body_bytes)
    command = {"type": "request", "request_id": "1:1", "headers": [["accept", "text/event-stream"]]}

    line = b"".join(native_egress_module._command_line_pieces(command, base64_body=body))

    assert line.endswith(b"\n") and line.count(b"\n") == 1
    decoded = json.loads(line)
    assert base64.b64decode(decoded.pop("body"), validate=True) == body
    assert decoded == command
    assert b"".join(native_egress_module._command_line_pieces(command, base64_body=None)) == (
        json.dumps(command, separators=(",", ":")).encode() + b"\n"
    )
