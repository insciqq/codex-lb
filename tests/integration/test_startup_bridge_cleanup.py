from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import event, func, insert, select, text

import app.main as main_module
import app.modules.proxy.durable_bridge_repository as durable_repo_module
from app.core.config.settings import get_settings
from app.core.utils.time import utcnow
from app.db.models import (
    HttpBridgeOperationEvent,
    HttpBridgeOperationRecord,
    HttpBridgeSessionRecord,
    HttpBridgeSessionState,
)
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_lifespan_startup_purges_abandoned_ownerless_bridge_rows(db_setup, monkeypatch) -> None:
    del db_setup

    monkeypatch.setenv("CODEX_LB_HTTP_RESPONSES_SESSION_BRIDGE_ENABLED", "true")
    monkeypatch.setenv("CODEX_LB_HTTP_RESPONSES_SESSION_BRIDGE_INSTANCE_ID", "startup-instance")
    get_settings.cache_clear()

    now = utcnow()
    stale_time = now - timedelta(hours=3)
    async with SessionLocal() as session:
        session.add_all(
            [
                HttpBridgeSessionRecord(
                    session_key_kind="session_header",
                    session_key_value="sid-stale-ownerless-startup",
                    session_key_hash="hash-stale-ownerless-startup",
                    api_key_scope="__anonymous__",
                    owner_instance_id=None,
                    owner_epoch=1,
                    lease_expires_at=stale_time,
                    state=HttpBridgeSessionState.ACTIVE,
                    account_id=None,
                    model="gpt-5.4",
                    last_seen_at=stale_time,
                    closed_at=None,
                ),
                HttpBridgeSessionRecord(
                    session_key_kind="session_header",
                    session_key_value="sid-recent-ownerless-startup",
                    session_key_hash="hash-recent-ownerless-startup",
                    api_key_scope="__anonymous__",
                    owner_instance_id=None,
                    owner_epoch=1,
                    lease_expires_at=now + timedelta(minutes=5),
                    state=HttpBridgeSessionState.ACTIVE,
                    account_id=None,
                    model="gpt-5.4",
                    last_seen_at=now,
                    closed_at=None,
                ),
            ]
        )
        await session.commit()

    app = main_module.create_app()
    async with app.router.lifespan_context(app):
        async with SessionLocal() as session:
            remaining_keys = set(
                await session.scalars(
                    select(HttpBridgeSessionRecord.session_key_value).where(
                        HttpBridgeSessionRecord.session_key_value.in_(
                            [
                                "sid-stale-ownerless-startup",
                                "sid-recent-ownerless-startup",
                            ]
                        )
                    )
                )
            )

    assert remaining_keys == {"sid-recent-ownerless-startup"}


@pytest.mark.asyncio
async def test_startup_operation_retention_failure_records_sanitized_aggregate(monkeypatch, caplog) -> None:
    recorded = Mock()
    purge = AsyncMock(side_effect=RuntimeError("operation_id=secret SQL DELETE FROM transcript"))
    coordinator = SimpleNamespace(purge_operation_spool_batch=purge)

    monkeypatch.setattr(main_module, "DurableBridgeSessionCoordinator", lambda _session_factory: coordinator)
    monkeypatch.setattr(main_module, "_record_operation_retention_cleanup", recorded)

    with caplog.at_level("WARNING", logger=main_module.__name__):
        with pytest.raises(RuntimeError, match="startup retention failed") as captured:
            await main_module._purge_operation_spool_on_startup(retention_seconds=60.0)

    assert captured.value.__cause__ is None
    purge.assert_awaited_once()
    result = recorded.call_args.args[0]
    assert result.deleted_operations == 0
    assert result.batches == 0
    assert result.backlog_likely is True
    assert result.outcome == "failed"
    assert "error_type=RuntimeError" in caplog.text
    assert "operation_id=secret" not in caplog.text
    assert "DELETE FROM" not in caplog.text


@pytest.mark.asyncio
async def test_startup_operation_retention_success_logs_aggregate_without_prometheus(monkeypatch, caplog) -> None:
    recorded = Mock()
    purge = AsyncMock(return_value=SimpleNamespace(deleted_operations=0, selected_operations=0, budget_exhausted=False))
    coordinator = SimpleNamespace(purge_operation_spool_batch=purge)

    monkeypatch.setattr(main_module, "DurableBridgeSessionCoordinator", lambda _session_factory: coordinator)
    monkeypatch.setattr(main_module, "PROMETHEUS_AVAILABLE", False)
    monkeypatch.setattr(main_module, "_record_operation_retention_cleanup", recorded)

    with caplog.at_level("INFO", logger=main_module.__name__):
        assert await main_module._purge_operation_spool_on_startup(retention_seconds=60.0) == 0

    purge.assert_awaited_once()
    result = recorded.call_args.args[0]
    assert result.deleted_operations == 0
    assert result.batches == 1
    assert result.backlog_likely is False
    assert result.outcome == "completed"
    assert "deleted_operations=0 batches=1 outcome=completed backlog_likely=False" in caplog.text
    assert "duration_seconds=" in caplog.text


@pytest.mark.asyncio
async def test_startup_operation_retention_success_logs_aggregate_when_metrics_disabled(monkeypatch, caplog) -> None:
    recorded = Mock()
    purge = AsyncMock(return_value=SimpleNamespace(deleted_operations=0, selected_operations=0, budget_exhausted=False))
    coordinator = SimpleNamespace(purge_operation_spool_batch=purge)

    monkeypatch.setattr(main_module, "DurableBridgeSessionCoordinator", lambda _session_factory: coordinator)
    monkeypatch.setattr(main_module, "PROMETHEUS_AVAILABLE", True)
    monkeypatch.setattr(main_module, "operation_retention_metrics_enabled", lambda: False)
    monkeypatch.setattr(main_module, "_record_operation_retention_cleanup", recorded)

    with caplog.at_level("INFO", logger=main_module.__name__):
        assert await main_module._purge_operation_spool_on_startup(retention_seconds=60.0) == 0

    recorded.assert_called_once()
    assert "deleted_operations=0 batches=1 outcome=completed backlog_likely=False" in caplog.text


@pytest.mark.asyncio
async def test_startup_operation_retention_stops_at_the_time_budget_and_reports_backlog(monkeypatch) -> None:
    recorded = Mock()
    purge = AsyncMock(return_value=SimpleNamespace(deleted_operations=2, selected_operations=5, budget_exhausted=True))
    coordinator = SimpleNamespace(purge_operation_spool_batch=purge)

    monkeypatch.setattr(main_module, "DurableBridgeSessionCoordinator", lambda _session_factory: coordinator)
    monkeypatch.setattr(main_module, "_record_operation_retention_cleanup", recorded)

    assert await main_module._purge_operation_spool_on_startup(retention_seconds=60.0) == 2

    assert purge.await_args is not None
    assert purge.await_args.kwargs["deadline"] is not None
    assert purge.await_args.kwargs["pace_writer"] is False
    result = recorded.call_args.args[0]
    assert result.backlog_likely is True
    assert result.outcome == "time_budget_exhausted"


@pytest.mark.asyncio
async def test_startup_operation_retention_purges_without_pausing_for_writers(db_setup, monkeypatch) -> None:
    del db_setup
    stale_at = utcnow() - timedelta(days=8)
    async with SessionLocal() as session:
        session.add(
            HttpBridgeSessionRecord(
                id="startup-retention-session",
                session_key_kind="session_header",
                session_key_value="startup-retention-session",
                session_key_hash="startup-retention-session",
                api_key_scope="__anonymous__",
            )
        )
        await session.flush()
        session.add(
            HttpBridgeOperationRecord(
                operation_id="startup-retention-operation",
                session_id="startup-retention-session",
                request_fingerprint="startup-retention-fingerprint",
                state="completed",
                created_at=stale_at,
                updated_at=stale_at,
            )
        )
        await session.flush()
        await session.execute(
            insert(HttpBridgeOperationEvent),
            [
                {
                    "event_id": f"startup-retention-event-{sequence}",
                    "operation_id": "startup-retention-operation",
                    "sequence_number": sequence,
                    "event_fingerprint": f"startup-retention-fingerprint-{sequence}",
                    "event_text": "data: {}\n\n",
                }
                for sequence in range(1, 600)
            ],
        )
        await session.commit()
    pauses: list[float] = []

    async def record_pause(delay: float, result: object = None) -> object:
        pauses.append(delay)
        return result

    monkeypatch.setattr(
        durable_repo_module,
        "scheduler_for",
        lambda _owner: SimpleNamespace(sleep=record_pause, create_task=asyncio.create_task, wait=asyncio.wait),
    )

    assert await main_module._purge_operation_spool_on_startup(retention_seconds=7 * 24 * 3600) == 1

    # Startup runs before any request writer exists, so it never pauses.
    assert pauses == []
    async with SessionLocal() as session:
        assert await session.get(HttpBridgeOperationRecord, "startup-retention-operation") is None
        remaining = await session.scalar(
            select(func.count())
            .select_from(HttpBridgeOperationEvent)
            .where(HttpBridgeOperationEvent.operation_id == "startup-retention-operation")
        )
        assert remaining == 0


async def _seed_expired_operation(operation_id: str, *, age_days: int) -> None:
    stale_at = utcnow() - timedelta(days=age_days)
    async with SessionLocal() as session:
        session.add(
            HttpBridgeSessionRecord(
                id=f"session-{operation_id}",
                session_key_kind="session_header",
                session_key_value=f"session-{operation_id}",
                session_key_hash=f"session-{operation_id}",
                api_key_scope="__anonymous__",
            )
        )
        await session.flush()
        session.add(
            HttpBridgeOperationRecord(
                operation_id=operation_id,
                session_id=f"session-{operation_id}",
                request_fingerprint=f"fingerprint-{operation_id}",
                state="completed",
                created_at=stale_at,
                updated_at=stale_at,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_partial_startup_operation_retention_failure_reports_committed_deletes(db_setup, monkeypatch) -> None:
    del db_setup
    if SessionLocal.kw["bind"].dialect.name != "sqlite":
        pytest.skip("uses a SQLite trigger to fail one delete")
    await _seed_expired_operation("startup-first", age_days=9)
    await _seed_expired_operation("startup-second", age_days=8)
    recorded = Mock()
    monkeypatch.setattr(main_module, "_record_operation_retention_cleanup", recorded)
    async with SessionLocal() as session:
        await session.execute(
            text(
                "CREATE TRIGGER block_second_operation_delete BEFORE DELETE ON http_bridge_operations "
                "WHEN old.operation_id = 'startup-second' BEGIN SELECT RAISE(ABORT, 'blocked'); END"
            )
        )
        await session.commit()
    try:
        with pytest.raises(RuntimeError, match="startup retention failed"):
            await main_module._purge_operation_spool_on_startup(retention_seconds=7 * 24 * 3600)
    finally:
        async with SessionLocal() as session:
            await session.execute(text("DROP TRIGGER block_second_operation_delete"))
            await session.commit()

    result = recorded.call_args.args[0]
    assert result.outcome == "failed"
    assert result.deleted_operations == 1
    async with SessionLocal() as session:
        assert await session.get(HttpBridgeOperationRecord, "startup-first") is None
        assert await session.get(HttpBridgeOperationRecord, "startup-second") is not None


@pytest.mark.asyncio
async def test_cancelled_startup_operation_retention_reports_committed_deletes(db_setup, monkeypatch) -> None:
    del db_setup
    await _seed_expired_operation("startup-first", age_days=9)
    await _seed_expired_operation("startup-second", age_days=8)
    recorded = Mock()
    monkeypatch.setattr(main_module, "_record_operation_retention_cleanup", recorded)
    sync_engine = SessionLocal.kw["bind"].sync_engine

    def cancel_on_second_delete(_conn, _cursor, statement: str, parameters, _context, _executemany) -> None:
        if statement.startswith("DELETE ") and "startup-second" in str(parameters):
            task = asyncio.current_task()
            assert task is not None
            task.cancel()

    event.listen(sync_engine, "before_cursor_execute", cancel_on_second_delete)
    try:
        with pytest.raises(asyncio.CancelledError):
            await main_module._purge_operation_spool_on_startup(retention_seconds=7 * 24 * 3600)
    finally:
        event.remove(sync_engine, "before_cursor_execute", cancel_on_second_delete)

    result = recorded.call_args.args[0]
    assert result.outcome == "failed"
    assert result.deleted_operations == 1
    async with SessionLocal() as session:
        assert await session.get(HttpBridgeOperationRecord, "startup-first") is None
