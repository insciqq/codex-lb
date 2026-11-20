"""Graceful shutdown must let an admitted response.create dump finish.

The dump is written in a worker thread. Under uvicorn the process exits right
after the lifespan returns, so a write still running then would leave a
payload without its ``.meta.json``.
"""

from __future__ import annotations

import asyncio
import threading
from importlib import import_module

import pytest
from fastapi.testclient import TestClient

import app.modules.proxy.service as proxy_service
from app.modules.proxy._service import response_create

pytestmark = pytest.mark.integration

app_main = import_module("app.main")

_WRITER_HOLD_SECONDS = 5.0
_RELEASE_AFTER_SECONDS = 0.5


def test_lifespan_finishes_dump(app_instance, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(proxy_service, "_OVERSIZED_RESPONSE_CREATE_DUMP_DIR", tmp_path)
    release = threading.Event()
    real_writer = response_create._write_response_create_dump

    def held_writer(*args, **kwargs):
        release.wait(_WRITER_HOLD_SECONDS)
        return real_writer(*args, **kwargs)

    monkeypatch.setattr(proxy_service, "_write_response_create_dump", held_writer)

    pending: list[asyncio.Task[bool]] = []
    done_when_shutdown_moved_on: list[bool] = []
    real_control_plane_drain = app_main._drain_detached_control_plane_tasks

    async def observe_later_shutdown_step(timeout_seconds: float) -> bool:
        # Runs after the proxy drains; by now the dump must be complete.
        done_when_shutdown_moved_on.append(all(task.done() for task in pending))
        return await real_control_plane_drain(timeout_seconds)

    monkeypatch.setattr(app_main, "_drain_detached_control_plane_tasks", observe_later_shutdown_step)

    async def submit_dump() -> list[asyncio.Task[bool]]:
        state = proxy_service._WebSocketRequestState(
            request_id="req_shutdown_dump",
            model="gpt-5.5",
            service_tier=None,
            reasoning_effort=None,
            api_key_reservation=None,
            started_at=0.0,
            awaiting_response_created=True,
            request_text='{"type":"response.create","input":"shutdown"}',
        )
        response_create._submit_response_create_dump(
            state,
            account_id_value=None,
            error_code="payload_too_large",
            error_message=None,
            log_prefix="guarded",
        )
        return list(response_create._response_create_dump_tasks)

    with TestClient(app_instance) as client:
        portal = client.portal
        assert portal is not None
        pending.extend(portal.call(submit_dump))
        assert len(pending) == 1
        threading.Timer(_RELEASE_AFTER_SECONDS, release.set).start()

    assert done_when_shutdown_moved_on == [True]
    assert len(list(tmp_path.glob("*.response-create.json.gz"))) == 1
    assert len(list(tmp_path.glob("*.meta.json"))) == 1
