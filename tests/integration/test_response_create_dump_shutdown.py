"""Graceful shutdown settles response.create dumps last, within what time is left.

A dump is written in a worker thread. Shutdown lets pending dumps finish only
after the essential teardown (API-key flush, leader release, database close),
and only within what remains of the shared shutdown deadline minus a margin,
so a debug dump can never make the server force the process to exit.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator
from importlib import import_module
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

import app.modules.proxy.service as proxy_service
from app.core import shutdown as shutdown_state
from app.core.server import GracefulDrainServer
from app.modules.proxy._service import response_create

pytestmark = pytest.mark.integration

app_main = import_module("app.main")

_WRITER_HOLD_SECONDS = 5.0
_RELEASE_AFTER_SECONDS = 0.5


@pytest.fixture
def held_writer(monkeypatch, tmp_path) -> Iterator[threading.Event]:
    monkeypatch.setattr(proxy_service, "_OVERSIZED_RESPONSE_CREATE_DUMP_DIR", tmp_path)
    release = threading.Event()
    real_writer = response_create._write_response_create_dump

    def writer(*args: Any, **kwargs: Any) -> bool:
        release.wait(_WRITER_HOLD_SECONDS)
        return real_writer(*args, **kwargs)

    monkeypatch.setattr(proxy_service, "_write_response_create_dump", writer)
    yield release
    release.set()


def _submit_dump(request_id: str) -> list[asyncio.Task[bool]]:
    state = proxy_service._WebSocketRequestState(
        request_id=request_id,
        model="gpt-5.5",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=0.0,
        awaiting_response_created=True,
        request_text='{"type":"response.create","input":"' + request_id + '"}',
    )
    response_create._submit_response_create_dump(
        state,
        account_id_value=None,
        error_code="payload_too_large",
        error_message=None,
        log_prefix="guarded",
    )
    return list(response_create._response_create_dump_tasks)


def _record_teardown_order(monkeypatch) -> list[str]:
    """Record teardown order; the dump step also records its budget and the time left."""
    order: list[str] = []
    real_close = app_main._close_db_and_record_clean_shutdown
    real_drain = app_main.drain_response_create_dumps

    async def close_db(**kwargs: Any) -> None:
        order.append("db_closed")
        await real_close(**kwargs)

    async def drain_dumps(timeout_seconds: float) -> Any:
        order.append("dumps")
        budgets.append((timeout_seconds, shutdown_state.remaining_post_drain_cleanup_timeout_seconds()))
        return await real_drain(timeout_seconds)

    monkeypatch.setattr(app_main, "_close_db_and_record_clean_shutdown", close_db)
    monkeypatch.setattr(app_main, "drain_response_create_dumps", drain_dumps)
    return order


budgets: list[tuple[float, float | None]] = []


def test_lifespan_finishes_dump(app_instance, monkeypatch, tmp_path, held_writer) -> None:
    order = _record_teardown_order(monkeypatch)
    pending: list[asyncio.Task[bool]] = []
    done_when_lifespan_completed: list[bool] = []
    real_mark_completed = shutdown_state.mark_lifespan_completed

    def mark_completed() -> None:
        done_when_lifespan_completed.append(all(task.done() for task in pending))
        real_mark_completed()

    monkeypatch.setattr(shutdown_state, "mark_lifespan_completed", mark_completed)

    async def submit() -> list[asyncio.Task[bool]]:
        return _submit_dump("req_shutdown_dump")

    with TestClient(app_instance) as client:
        portal = client.portal
        assert portal is not None
        pending.extend(portal.call(submit))
        assert len(pending) == 1
        threading.Timer(_RELEASE_AFTER_SECONDS, held_writer.set).start()

    assert order == ["db_closed", "dumps"], "debug dumps must wait until the database is closed"
    assert done_when_lifespan_completed == [True]
    assert len(list(tmp_path.glob("*.response-create.json.gz"))) == 1
    assert len(list(tmp_path.glob("*.meta.json"))) == 1


@pytest.mark.asyncio
async def test_running_dump_never_starves_essential_teardown(app_instance, monkeypatch, held_writer, caplog) -> None:
    """A dump still writing at the deadline is reported, not waited for into a forced exit."""
    caplog.set_level(logging.WARNING, logger=app_main.logger.name)
    order = _record_teardown_order(monkeypatch)
    forced_exit_signals: list[int] = []
    monkeypatch.setattr("app.core.server._force_process_exit", forced_exit_signals.append)

    lifespan = app_main.lifespan(app_instance)
    await lifespan.__aenter__()
    pending = _submit_dump("req_still_writing")
    await asyncio.sleep(0.05)  # the held write has started

    async def base_shutdown(_server: uvicorn.Server, sockets: list[Any] | None = None) -> None:
        del sockets
        await lifespan.__aexit__(None, None, None)

    monkeypatch.setattr(uvicorn.Server, "shutdown", base_shutdown)
    server = GracefulDrainServer(
        uvicorn.Config(app_instance, timeout_graceful_shutdown=None),
        drain_timeout_seconds=0.05,
        post_drain_cleanup_timeout_seconds=0.45,
    )
    try:
        await asyncio.wait_for(server.shutdown(), timeout=10)
    finally:
        held_writer.set()
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=_WRITER_HOLD_SECONDS)
        shutdown_state.reset()

    assert forced_exit_signals == []
    assert order == ["db_closed", "dumps"]
    # Dumps get only what is left before the shared deadline minus the margin.
    timeout_seconds, remaining = budgets[-1]
    assert remaining is not None
    assert timeout_seconds <= max(0.0, remaining - app_main._RESPONSE_CREATE_DUMP_SHUTDOWN_MARGIN_SECONDS) + 0.01
    assert any(
        "still being written at shutdown request_id=req_still_writing" in record.getMessage()
        for record in caplog.records
    )
