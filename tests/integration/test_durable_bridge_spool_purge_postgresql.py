"""Operation spool retention never deadlocks with the request path on PostgreSQL.

``record_operation`` locks the owning session row before the operation row.
Retention rechecks eligibility with a joined ``FOR UPDATE`` that reaches the
operation first, so waiting for the session row there closed a lock cycle and
PostgreSQL aborted one side with "deadlock detected".
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from app.core.utils.time import utcnow
from app.db.models import HttpBridgeOperationRecord, HttpBridgeSessionRecord
from app.db.session import SessionLocal
from app.modules.proxy.durable_bridge_repository import (
    DurableBridgeOperationPurgeBatchResult,
    DurableBridgeOperationSnapshot,
    DurableBridgeRepository,
    durable_bridge_hash,
    durable_bridge_operation_id,
)

pytestmark = pytest.mark.integration


def _is_postgresql_database_url(url: str) -> bool:
    return url.startswith("postgresql+")


@pytest.mark.skipif(
    not _is_postgresql_database_url(os.environ.get("CODEX_LB_TEST_DATABASE_URL", "")),
    reason="PostgreSQL-only row-lock ordering test",
)
@pytest.mark.asyncio
async def test_spool_purge_overlapping_record_operation_finishes_without_deadlock(db_setup) -> None:
    del db_setup
    async with SessionLocal() as setup:
        repository = DurableBridgeRepository(setup)
        claim = await repository.claim_session(
            session_key_kind="session_header",
            session_key_value="sid-retention-lock-order",
            api_key_scope="__anonymous__",
            instance_id="inst-retention-lock-order",
            owner_process_epoch="test-process",
            lease_ttl_seconds=120.0,
            account_id=None,
            model="gpt-5.4",
            service_tier=None,
            latest_turn_state=None,
            latest_response_id=None,
            allow_takeover=False,
        )
        assert claim is not None
        fingerprint = durable_bridge_hash("retention-lock-order")
        operation_id = durable_bridge_operation_id(claim.id, fingerprint)
        session_id, owner_epoch = claim.id, claim.owner_epoch

        async def record(target: DurableBridgeRepository) -> DurableBridgeOperationSnapshot | None:
            return await target.record_operation(
                operation_id=operation_id,
                session_id=session_id,
                instance_id="inst-retention-lock-order",
                owner_epoch=owner_epoch,
                request_fingerprint=fingerprint,
                account_id=None,
                model="gpt-5.4",
                parent_response_id=None,
                request_text='{"input":"duplicate"}',
            )

        assert await record(repository) is not None
        await setup.execute(
            update(HttpBridgeOperationRecord)
            .where(HttpBridgeOperationRecord.operation_id == operation_id)
            .values(state="completed", updated_at=utcnow() - timedelta(days=8))
        )
        await setup.commit()

    session_locked = asyncio.Event()
    release_request = asyncio.Event()

    async def duplicate_request() -> DurableBridgeOperationSnapshot | None:
        async with SessionLocal() as session:
            original_scalar = session.scalar
            calls = 0

            async def scalar_pausing_after_the_session_lock(*args: Any, **kwargs: Any) -> Any:
                nonlocal calls
                result = await original_scalar(*args, **kwargs)
                calls += 1
                if calls == 1:
                    session_locked.set()
                    await release_request.wait()
                return result

            setattr(session, "scalar", scalar_pausing_after_the_session_lock)
            return await record(DurableBridgeRepository(session))

    selection_committed = asyncio.Event()
    request_holds_session = asyncio.Event()

    async def retention() -> DurableBridgeOperationPurgeBatchResult:
        async with SessionLocal() as session:
            original_scalar = session.scalar
            calls = 0

            async def scalar_waiting_for_the_request(*args: Any, **kwargs: Any) -> Any:
                # The selection runs through execute() and has committed by
                # now; the first scalar() is the per-transaction recheck.
                nonlocal calls
                calls += 1
                if calls == 1:
                    selection_committed.set()
                    await request_holds_session.wait()
                return await original_scalar(*args, **kwargs)

            setattr(session, "scalar", scalar_waiting_for_the_request)
            return await DurableBridgeRepository(session).purge_operation_spool_batch(cutoff=utcnow())

    retention_task = asyncio.create_task(retention())
    request_task: asyncio.Task[DurableBridgeOperationSnapshot | None] | None = None
    try:
        await asyncio.wait_for(selection_committed.wait(), timeout=10)
        # The request locks the session after selection and before the
        # recheck, so the recheck itself meets the lock.
        request_task = asyncio.create_task(duplicate_request())
        await asyncio.wait_for(session_locked.wait(), timeout=10)
        request_holds_session.set()
        # Retention must finish while the request still holds the session
        # row: waiting for it is what closed the lock cycle.
        purge_result = await asyncio.wait_for(retention_task, timeout=10)
    finally:
        request_holds_session.set()
        release_request.set()
        snapshot = await asyncio.wait_for(request_task, timeout=30) if request_task is not None else None
        if not retention_task.done():
            retention_task.cancel()

    assert (purge_result.selected_operations, purge_result.deleted_operations) == (1, 0)
    assert snapshot is not None
    # The contended operation was left to a later pass, not deleted under the
    # request that was reading it.
    async with SessionLocal() as session:
        later = await DurableBridgeRepository(session).purge_operation_spool_batch(cutoff=utcnow())
        assert later.deleted_operations == 1


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


@pytest.mark.skipif(
    not _is_postgresql_database_url(os.environ.get("CODEX_LB_TEST_DATABASE_URL", "")),
    reason="PostgreSQL-only row-lock selection test",
)
@pytest.mark.asyncio
async def test_spool_purge_fills_the_batch_past_a_locked_session(db_setup) -> None:
    del db_setup
    await _seed_expired_operation("old", age_days=9)
    await _seed_expired_operation("next", age_days=8)

    async with SessionLocal() as holder:
        # A request holds the oldest operation's session row.
        await holder.execute(
            select(HttpBridgeSessionRecord.id).where(HttpBridgeSessionRecord.id == "session-old").with_for_update()
        )
        async with SessionLocal() as session:
            result = await asyncio.wait_for(
                DurableBridgeRepository(session).purge_operation_spool_batch(cutoff=utcnow(), batch_size=1),
                timeout=10,
            )
        await holder.rollback()

    # The locked operation does not take the only batch slot.
    assert (result.selected_operations, result.deleted_operations) == (1, 1)
    async with SessionLocal() as session:
        assert await session.get(HttpBridgeOperationRecord, "next") is None
        assert await session.get(HttpBridgeOperationRecord, "old") is not None
