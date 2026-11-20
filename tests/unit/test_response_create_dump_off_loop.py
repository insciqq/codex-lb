"""A response.create debug dump must not run on the event loop.

Dumping hashes and gzip-compresses the whole request text. For a payload near
the upstream response.create limit that is seconds of CPU, and when it ran on
the loop every other request on the instance stalled for that long.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

import pytest

import app.modules.proxy.service as proxy_service
from app.core.clients.proxy import ProxyResponseError
from app.modules.proxy._service import response_create

pytestmark = pytest.mark.unit

_WRITER_HOLD_SECONDS = 5.0


def _request_state(request_id: str, text: str) -> proxy_service._WebSocketRequestState:
    return proxy_service._WebSocketRequestState(
        request_id=request_id,
        model="gpt-5.5",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=0.0,
        awaiting_response_created=True,
        request_text=text,
    )


def _payload(fill: str) -> str:
    return '{"type":"response.create","input":"' + fill * 128 + '"}'


class _HeldWriter:
    """Stands in for the seconds of hashing and gzip on a large payload."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.threads: list[int] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        self._real = response_create._write_response_create_dump

    def __call__(self, *args: Any, **kwargs: Any) -> bool:
        with self._lock:
            self.threads.append(threading.get_ident())
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.release.wait(_WRITER_HOLD_SECONDS)
            return self._real(*args, **kwargs)
        finally:
            with self._lock:
                self.active -= 1


@pytest.fixture
def dump_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy_service, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(proxy_service, "_UPSTREAM_RESPONSE_CREATE_MAX_BYTES", 64)
    monkeypatch.setattr(proxy_service, "_UPSTREAM_RESPONSE_CREATE_WARN_BYTES", 32)
    return tmp_path / "debug" / "response-create-dumps"


@pytest.fixture
def held_writer(monkeypatch) -> _HeldWriter:
    writer = _HeldWriter()
    monkeypatch.setattr(proxy_service, "_write_response_create_dump", writer)
    return writer


def _reject(request_id: str, text: str) -> ProxyResponseError:
    with pytest.raises(ProxyResponseError) as rejected:
        response_create._enforce_response_create_size_limit(_request_state(request_id, text))
    return rejected.value


async def test_guarded_dumps_are_written_off_the_loop_one_at_a_time(dump_dir, held_writer) -> None:
    loop_thread = threading.get_ident()

    started = time.monotonic()
    first = _reject("req_first", _payload("a"))
    second = _reject("req_second", _payload("b"))
    elapsed = time.monotonic() - started

    assert first.status_code == 400 and second.status_code == 400
    assert elapsed < _WRITER_HOLD_SECONDS / 2, "the dump write blocked the caller on the event loop"

    pending = list(response_create._response_create_dump_tasks)
    assert len(pending) == 2
    held_writer.release.set()
    await asyncio.wait_for(asyncio.gather(*pending), timeout=_WRITER_HOLD_SECONDS)

    assert len(held_writer.threads) == 2
    assert loop_thread not in held_writer.threads
    assert held_writer.max_active == 1, "dumps must be written one at a time"
    # Distinct payloads each get their own pair.
    assert len(list(dump_dir.glob("*.response-create.json.gz"))) == 2
    assert len(list(dump_dir.glob("*.meta.json"))) == 2
    assert not response_create._response_create_dump_tasks


async def test_identical_retry_queued_behind_its_dump_is_deduplicated(dump_dir, held_writer, caplog) -> None:
    caplog.set_level(logging.WARNING, logger=response_create.logger.name)
    text = _payload("c")

    _reject("req_original", text)
    _reject("req_retry", text)
    pending = list(response_create._response_create_dump_tasks)
    held_writer.release.set()
    await asyncio.wait_for(asyncio.gather(*pending), timeout=_WRITER_HOLD_SECONDS)

    assert len(list(dump_dir.glob("*.response-create.json.gz"))) == 1
    fingerprint = sha256(text.encode("utf-8")).hexdigest()
    duplicate = [r.getMessage() for r in caplog.records if "Skipped duplicate" in r.getMessage()]
    assert len(duplicate) == 1
    assert "req_retry" in duplicate[0] and fingerprint in duplicate[0] and "existing_dump_path=" in duplicate[0]


async def test_dump_beyond_the_pending_count_is_dropped_with_its_size(
    dump_dir, held_writer, monkeypatch, caplog
) -> None:
    caplog.set_level(logging.WARNING, logger=response_create.logger.name)
    monkeypatch.setattr(response_create, "_RESPONSE_CREATE_DUMP_MAX_PENDING", 2)

    _reject("req_one", _payload("d"))
    _reject("req_two", _payload("e"))
    dropped_text = _payload("f")
    dropped = _reject("req_three", dropped_text)

    assert dropped.status_code == 400
    pending = list(response_create._response_create_dump_tasks)
    assert len(pending) == 2
    held_writer.release.set()
    await asyncio.wait_for(asyncio.gather(*pending), timeout=_WRITER_HOLD_SECONDS)

    assert len(list(dump_dir.glob("*.response-create.json.gz"))) == 2
    messages = [r.getMessage() for r in caplog.records if "Dropped" in r.getMessage()]
    assert len(messages) == 1
    assert "req_three" in messages[0]
    assert f"retained_bytes={sys.getsizeof(dropped_text)}" in messages[0]


async def test_large_payload_dump_skips_the_whole_payload_summary(dump_dir, monkeypatch) -> None:
    monkeypatch.setattr(proxy_service, "_RESPONSE_CREATE_DUMP_SUMMARY_MAX_BYTES", 100, raising=False)
    parsed: list[str] = []
    real_loads = json.loads

    def tracking_loads(text: Any, *args: Any, **kwargs: Any) -> Any:
        parsed.append(text if isinstance(text, str) else "")
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(response_create.json, "loads", tracking_loads)
    text = _payload("g")

    _reject("req_large", text)
    await asyncio.wait_for(asyncio.gather(*response_create._response_create_dump_tasks), timeout=_WRITER_HOLD_SECONDS)

    assert text not in parsed, "the whole payload must not be parsed for the summary"
    (meta_path,) = dump_dir.glob("*.meta.json")
    meta = real_loads(meta_path.read_text(encoding="utf-8"))
    assert meta["summary"] == {"skipped": "payload larger than the summary limit", "limit_bytes": 100}
    assert meta["request"]["request_text_bytes"] == len(text)


async def test_drain_reports_a_dump_still_being_written(dump_dir, held_writer) -> None:
    _reject("req_drain", _payload("h"))

    assert await response_create.drain_response_create_dumps(0.05) is False
    held_writer.release.set()
    assert await response_create.drain_response_create_dumps(_WRITER_HOLD_SECONDS) is True
    assert await response_create.drain_response_create_dumps(0) is True


async def test_pending_dumps_are_bounded_by_retained_bytes(monkeypatch, caplog) -> None:
    """Ingress admits bodies far above the upstream limit, so a count bound alone can pin hundreds of MiB."""
    from app.core.openai.requests import ResponsesRequest
    from app.modules.proxy._service.http_bridge.request_submit import _HTTPBridgeRequestSubmitMixin

    caplog.set_level(logging.WARNING, logger=response_create.logger.name)
    monkeypatch.setattr(proxy_service, "_write_response_create_dump", lambda *args, **kwargs: True)
    retained_sources: list[Any] = []
    real_write_after = response_create._write_response_create_dump_after

    def recording_write_after(previous: Any, write: Any, source: Any, **kwargs: Any) -> Any:
        retained_sources.append(source)
        return real_write_after(previous, write, source, **kwargs)

    monkeypatch.setattr(response_create, "_write_response_create_dump_after", recording_write_after)
    mixin = _HTTPBridgeRequestSubmitMixin()
    size = 20 << 20

    for index in range(4):
        payload = ResponsesRequest.model_validate(
            {
                "model": "gpt-5.1",
                "instructions": "",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": str(index) * size}]}],
            }
        )
        with pytest.raises(ProxyResponseError):
            mixin._prepare_http_bridge_request(payload, {}, api_key=None, api_key_reservation=None)

    pending = list(response_create._response_create_dump_tasks)
    retained = sum(len(source.request_text or "") for source in retained_sources)
    try:
        assert retained <= response_create._RESPONSE_CREATE_DUMP_MAX_PENDING_BYTES
        assert len(pending) == 3
        dropped = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Dropped")]
        assert len(dropped) == 1 and "retained_bytes=" in dropped[0]
    finally:
        await asyncio.gather(*pending)
    assert not response_create._response_create_dump_retained_bytes


async def test_payload_larger_than_the_byte_budget_is_not_retained(dump_dir, monkeypatch, caplog) -> None:
    caplog.set_level(logging.WARNING, logger=response_create.logger.name)
    monkeypatch.setattr(response_create, "_RESPONSE_CREATE_DUMP_MAX_PENDING_BYTES", 256)
    encoded: list[int] = []
    real_sha256 = response_create.sha256

    def tracking_sha256(data: bytes = b"") -> Any:
        encoded.append(len(data))
        return real_sha256(data)

    monkeypatch.setattr(response_create, "sha256", tracking_sha256)

    _reject("req_huge", _payload("i") * 4)

    assert not response_create._response_create_dump_tasks
    assert encoded == [], "the drop path must not hash the payload on the loop"
    assert any("Dropped" in r.getMessage() and "req_huge" in r.getMessage() for r in caplog.records)
