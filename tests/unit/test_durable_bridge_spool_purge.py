"""Operation spool retention keeps every SQLite write transaction small.

A whole transcript deleted in one transaction held SQLite's single writer slot
for as long as the delete took, which on a large database ran past
``busy_timeout`` and failed every other writer with "database is locked".
These tests run the purge against file-backed SQLite with production pragmas
and pool, and measure each retention transaction by the transcript rows that
are gone after it commits, so ON DELETE CASCADE removals are counted too.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import aiosqlite
import pytest
from sqlalchemy import event, insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.db.session as session_module
import app.modules.proxy.durable_bridge_repository as durable_repo_module
import app.modules.sticky_sessions.cleanup_scheduler as cleanup_scheduler
from app.core.utils.time import utcnow
from app.db.models import (
    HTTP_BRIDGE_SPOOL_FORMAT_CHUNKS_V2,
    Base,
    HttpBridgeOperationEvent,
    HttpBridgeOperationEventChunk,
    HttpBridgeOperationRecord,
    HttpBridgeSessionRecord,
)
from app.db.session import sqlite_writer_section
from app.modules.proxy.durable_bridge_repository import (
    DURABLE_BRIDGE_OPERATION_SPOOL_PURGE_CHUNK_ROWS_PER_TRANSACTION,
    DURABLE_BRIDGE_OPERATION_SPOOL_PURGE_EVENT_ROWS_PER_TRANSACTION,
    DURABLE_BRIDGE_OPERATION_SPOOL_PURGE_MAX_YIELD_SECONDS,
    DurableBridgeOperationPurgeProgress,
    DurableBridgeRepository,
)

pytestmark = pytest.mark.asyncio

_EVENT_LIMIT = DURABLE_BRIDGE_OPERATION_SPOOL_PURGE_EVENT_ROWS_PER_TRANSACTION
_CHUNK_LIMIT = DURABLE_BRIDGE_OPERATION_SPOOL_PURGE_CHUNK_ROWS_PER_TRANSACTION
_EVENT_ROWS = 3 * _EVENT_LIMIT + 7
_CHUNK_ROWS = 2 * _CHUNK_LIMIT + 3

_Database = tuple[async_sessionmaker[AsyncSession], Path]


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[_Database]:
    path = tmp_path / "spool.db"
    engine: AsyncEngine = create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_connection: sqlite3.Connection, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False), path
    await engine.dispose()


async def _seed_operation(
    session: AsyncSession,
    *,
    operation_id: str,
    state: str = "completed",
    events: int = 0,
    chunks: int = 0,
) -> None:
    now = utcnow()
    session_id = f"session-{operation_id}"
    session.add(
        HttpBridgeSessionRecord(
            id=session_id,
            session_key_kind="session_header",
            session_key_value=session_id,
            session_key_hash=session_id,
            api_key_scope="__anonymous__",
            owner_instance_id=None,
            lease_expires_at=None,
        )
    )
    await session.flush()
    stale_at = now - timedelta(days=8)
    session.add(
        HttpBridgeOperationRecord(
            operation_id=operation_id,
            session_id=session_id,
            request_fingerprint=f"fingerprint-{operation_id}",
            request_text='{"input":"retained request"}',
            state=state,
            spool_format=HTTP_BRIDGE_SPOOL_FORMAT_CHUNKS_V2 if chunks else "rows_v1",
            created_at=stale_at,
            updated_at=stale_at,
        )
    )
    await session.flush()
    if events:
        await session.execute(
            insert(HttpBridgeOperationEvent),
            [
                {
                    "event_id": f"{operation_id}-event-{sequence}",
                    "operation_id": operation_id,
                    "sequence_number": sequence,
                    "event_fingerprint": f"{operation_id}-fingerprint-{sequence}",
                    "event_text": "data: {}\n\n",
                }
                for sequence in range(1, events + 1)
            ],
        )
    if chunks:
        await session.execute(
            insert(HttpBridgeOperationEventChunk),
            [
                {
                    "operation_id": operation_id,
                    "first_sequence_number": sequence,
                    "event_count": 1,
                    "codec": "zstd",
                    "uncompressed_bytes": 1,
                    "payload": b"x",
                    "payload_sha256": "0" * 64,
                }
                for sequence in range(1, chunks + 1)
            ],
        )
    await session.commit()


def _scalar(path: Path, sql: str, *params: object) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute(sql, params).fetchone()[0])
    finally:
        connection.close()


def _spool_rows(path: Path, operation_id: str) -> int:
    return _scalar(
        path,
        "SELECT (SELECT count(*) FROM http_bridge_operation_events WHERE operation_id = ?)"
        " + (SELECT count(*) FROM http_bridge_operation_event_chunks WHERE operation_id = ?)",
        operation_id,
        operation_id,
    )


def _operation_rows(path: Path, operation_id: str) -> int:
    return _scalar(path, "SELECT count(*) FROM http_bridge_operations WHERE operation_id = ?", operation_id)


def _assert_writer_slot_free(path: Path) -> None:
    # A zero busy timeout fails at once if any connection still holds the
    # writer slot, which is exactly what every other writer observed.
    probe = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("ROLLBACK")
    finally:
        probe.close()


class _Pauses:
    """Scheduler seam double: records each pause instead of sleeping."""

    def __init__(self, *, cancel_when: Callable[[], bool] | None = None) -> None:
        self.delays: list[float] = []
        self._cancel_when = cancel_when

    # Tasks are not timers: the seam's spawn and wait members stay real.
    create_task = staticmethod(asyncio.create_task)
    wait = staticmethod(asyncio.wait)

    async def sleep(self, delay: float, result: object = None) -> object:
        self.delays.append(delay)
        if self._cancel_when is not None and self._cancel_when():
            raise asyncio.CancelledError
        return result


def _repository(
    session: AsyncSession,
    *,
    pauses: _Pauses | None = None,
    clock: object | None = None,
) -> DurableBridgeRepository:
    repository = DurableBridgeRepository(session)
    # The clock/scheduler seams that clock_for()/scheduler_for() read.
    setattr(repository, "_scheduler", pauses or _Pauses())
    if clock is not None:
        setattr(repository, "_clock", clock)
    return repository


@dataclass
class _Commit:
    spool_rows_removed: int
    operation_rows_removed: int


def _observe_commits(
    session: AsyncSession,
    path: Path,
    operation_ids: tuple[str, ...],
    *,
    after: Callable[[int], None] | None = None,
) -> list[_Commit]:
    """Measure every committed purge transaction by the rows gone after it.

    Counting remaining rows from another connection sees ON DELETE CASCADE
    removals that a cursor rowcount never reports. Each commit also proves the
    writer slot is free again.
    """

    commits: list[_Commit] = []

    def totals() -> tuple[int, int]:
        return (
            sum(_spool_rows(path, operation_id) for operation_id in operation_ids),
            sum(_operation_rows(path, operation_id) for operation_id in operation_ids),
        )

    previous = [totals()]

    def after_commit(_session: object) -> None:
        current = totals()
        commits.append(_Commit(previous[0][0] - current[0], previous[0][1] - current[1]))
        previous[0] = current
        _assert_writer_slot_free(path)
        if after is not None:
            after(len(commits))

    event.listen(session.sync_session, "after_commit", after_commit)
    return commits


def _assert_bounded(commits: list[_Commit]) -> None:
    assert commits, "the purge committed no transaction"
    for commit in commits:
        assert commit.spool_rows_removed <= max(_EVENT_LIMIT, _CHUNK_LIMIT), commit
        if commit.operation_rows_removed:
            # The operation delete never cascades into transcript rows.
            assert commit.spool_rows_removed == 0, commit


async def test_spool_purge_bounds_rows_per_write_transaction_and_releases_the_writer(database: _Database) -> None:
    session_factory, path = database
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-rows", events=_EVENT_ROWS)
        await _seed_operation(session, operation_id="op-chunks", chunks=_CHUNK_ROWS)
    statements: list[str] = []
    bind = session_factory.kw["bind"]
    event.listen(bind.sync_engine, "before_cursor_execute", lambda *args: statements.append(args[2]))

    async with session_factory() as session:
        commits = _observe_commits(session, path, ("op-rows", "op-chunks"))
        result = await _repository(session).purge_operation_spool_batch(cutoff=utcnow())

    assert (result.selected_operations, result.deleted_operations, result.budget_exhausted) == (2, 2, False)
    _assert_bounded(commits)
    removed = [commit.spool_rows_removed for commit in commits if commit.spool_rows_removed]
    assert removed == [
        *([_EVENT_LIMIT] * 3),
        _EVENT_ROWS - 3 * _EVENT_LIMIT,
        *([_CHUNK_LIMIT] * 2),
        _CHUNK_ROWS - 2 * _CHUNK_LIMIT,
    ]
    assert sum(commit.operation_rows_removed for commit in commits) == 2
    for operation_id in ("op-rows", "op-chunks"):
        assert _operation_rows(path, operation_id) == 0
        assert _spool_rows(path, operation_id) == 0
    # A cancelled RETURNING write can keep the SQLite write lock after its
    # connection closes, so retention deletes must not use one.
    deletes = [statement for statement in statements if statement.lstrip().upper().startswith("DELETE")]
    assert deletes
    assert all("RETURNING" not in statement.upper() for statement in deletes)


def _cancel_after_event_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    original_execute = aiosqlite.Cursor.execute

    async def execute_then_cancel(self: aiosqlite.Cursor, sql: str, parameters: Any = None) -> aiosqlite.Cursor:
        cursor = await original_execute(self, sql, parameters)
        if sql.lstrip().startswith("DELETE FROM http_bridge_operation_events"):
            # Lease loss cancels the leader-gated task right after SQLite ran
            # the delete, before the driver returned.
            raise asyncio.CancelledError
        return cursor

    monkeypatch.setattr(aiosqlite.Cursor, "execute", execute_then_cancel)


async def test_spool_purge_cancelled_after_a_delete_leaves_the_writer_free(
    database: _Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory, path = database
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-rows", events=_EVENT_ROWS)

    _cancel_after_event_delete(monkeypatch)
    session = session_factory()
    try:
        with pytest.raises(asyncio.CancelledError):
            await _repository(session).purge_operation_spool_batch(cutoff=utcnow())
    finally:
        await session.close()

    _assert_writer_slot_free(path)
    assert _operation_rows(path, "op-rows") == 1
    assert _spool_rows(path, "op-rows") == _EVENT_ROWS


async def test_scheduled_retention_cancelled_mid_purge_reports_progress_and_frees_the_writer(
    database: _Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory, path = database
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-small", events=3)
        await _seed_operation(session, operation_id="op-rows", events=_EVENT_ROWS)

    original_execute = aiosqlite.Cursor.execute
    event_deletes = 0

    async def cancel_on_second_operation(self: aiosqlite.Cursor, sql: str, parameters: Any = None) -> Any:
        nonlocal event_deletes
        cursor = await original_execute(self, sql, parameters)
        if sql.lstrip().startswith("DELETE FROM http_bridge_operation_events"):
            event_deletes += 1
            if event_deletes == 2:
                raise asyncio.CancelledError
        return cursor

    monkeypatch.setattr(aiosqlite.Cursor, "execute", cancel_on_second_operation)
    session = session_factory()
    try:
        with pytest.raises(cleanup_scheduler.OperationRetentionCleanupCancelledError) as captured:
            await cleanup_scheduler._purge_operation_spool_with_budget(_repository(session), cutoff=utcnow())
    finally:
        await session.close()

    _assert_writer_slot_free(path)
    # The first operation's committed delete is reported although the batch
    # never returned.
    assert captured.value.result.deleted_operations == 1
    assert captured.value.result.backlog_likely is True
    assert _operation_rows(path, "op-small") == 0
    assert _operation_rows(path, "op-rows") == 1


async def test_spool_purge_stops_when_the_operation_is_owned_again_mid_purge(database: _Database) -> None:
    session_factory, path = database
    async with session_factory() as session:
        # A stale nonterminal operation is purgeable only while its session
        # has no live owner.
        await _seed_operation(session, operation_id="op-unknown", state="unknown", events=_EVENT_ROWS)

    def reclaim(commit_count: int) -> None:
        if commit_count == 2:  # the id selection, then the first event delete
            _reclaim_session(path, "session-op-unknown", lease_seconds=300)

    async with session_factory() as session:
        commits = _observe_commits(session, path, ("op-unknown",), after=reclaim)
        result = await _repository(session).purge_operation_spool_batch(cutoff=utcnow())

    assert (result.selected_operations, result.deleted_operations) == (1, 0)
    assert [commit.spool_rows_removed for commit in commits] == [0, _EVENT_LIMIT, 0]
    # The duplicate-suppression row survives, and nothing past the first
    # bounded transaction was removed once the owner came back.
    assert _operation_rows(path, "op-unknown") == 1
    assert _spool_rows(path, "op-unknown") == _EVENT_ROWS - _EVENT_LIMIT


async def test_spool_purge_lease_expiring_between_transactions_never_cascades_the_spool(
    database: _Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory, path = database
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-unknown", state="unknown", events=_EVENT_ROWS)

    def reclaim_then_expire(commit_count: int) -> None:
        if commit_count == 2:
            # The owner comes back after the first event delete, and its lease
            # has expired by the time of the next transaction.
            _reclaim_session(path, "session-op-unknown", lease_seconds=300)
            later = utcnow() + timedelta(minutes=6)
            monkeypatch.setattr(durable_repo_module, "utcnow", lambda: later)

    async with session_factory() as session:
        commits = _observe_commits(session, path, ("op-unknown",), after=reclaim_then_expire)
        result = await _repository(session).purge_operation_spool_batch(cutoff=utcnow())

    # The operation is purgeable again, and the rest of its spool still goes
    # in bounded transactions: the operation delete removes no spool rows.
    assert result.deleted_operations == 1
    _assert_bounded(commits)
    assert sum(commit.spool_rows_removed for commit in commits) == _EVENT_ROWS
    assert _operation_rows(path, "op-unknown") == 0
    assert _spool_rows(path, "op-unknown") == 0


@pytest.mark.skipif(
    not session_module._is_sqlite_url(session_module._database_url),
    reason="the in-process SQLite writer gate is a no-op for other database URLs",
)
async def test_spool_purge_pause_matches_the_hold_after_the_writer_gate(database: _Database) -> None:
    session_factory, _path = database
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-rows", events=10)
    # Every clock read advances 10 ms; the test adds 5 s while the purge
    # queues for the writer gate.
    clock = _SteppingClock(0.01)

    gate_held = asyncio.Event()
    release_gate = asyncio.Event()

    async def hold_writer_gate() -> None:
        async with sqlite_writer_section():
            gate_held.set()
            await release_gate.wait()

    holder = asyncio.create_task(hold_writer_gate())
    await gate_held.wait()
    pauses = _Pauses()
    async with session_factory() as session:
        purge = asyncio.create_task(
            _repository(session, pauses=pauses, clock=clock).purge_operation_spool_batch(cutoff=utcnow())
        )
        await asyncio.sleep(0.2)
        clock.now += 5.0
        release_gate.set()
        result = await purge
    await holder

    assert result.deleted_operations == 1
    # One pause per committed write transaction (events, chunks, operation),
    # each as long as that transaction held the writer: the 5 s spent
    # queueing behind another writer is not paced.
    assert len(pauses.delays) == 3
    assert all(delay == pytest.approx(0.01) for delay in pauses.delays)


class _SteppingClock:
    def __init__(self, step: float) -> None:
        self.now = 1000.0
        self._step = step

    def monotonic(self) -> float:
        self.now += self._step
        return self.now


async def test_spool_purge_pause_is_capped(database: _Database) -> None:
    session_factory, _path = database
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-rows", events=10)

    pauses = _Pauses()
    async with session_factory() as session:
        # Every clock read advances 5 s, so each transaction "holds" 5 s.
        repository = _repository(session, pauses=pauses, clock=_SteppingClock(5.0))
        result = await repository.purge_operation_spool_batch(cutoff=utcnow())

    assert result.deleted_operations == 1
    assert pauses.delays == [DURABLE_BRIDGE_OPERATION_SPOOL_PURGE_MAX_YIELD_SECONDS] * 3


async def test_spool_purge_without_pacing_never_pauses(database: _Database) -> None:
    session_factory, path = database
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-rows", events=_EVENT_ROWS)

    pauses = _Pauses()
    async with session_factory() as session:
        result = await _repository(session, pauses=pauses).purge_operation_spool_batch(
            cutoff=utcnow(), pace_writer=False
        )

    assert result.deleted_operations == 1
    assert pauses.delays == []
    assert _spool_rows(path, "op-rows") == 0


async def test_spool_purge_never_pauses_on_postgresql() -> None:
    pauses = _Pauses()
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    repository = _repository(cast(AsyncSession, session), pauses=pauses)

    await repository._pace_spool_purge(0.5, enabled=True)

    assert pauses.delays == []


async def test_spool_purge_stops_between_transactions_at_the_deadline(database: _Database) -> None:
    session_factory, path = database
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-rows", events=_EVENT_ROWS)

    clock = _SteppingClock(0.0)

    def pass_deadline(commit_count: int) -> None:
        if commit_count == 2:
            clock.now = 2000.0

    async with session_factory() as session:
        commits = _observe_commits(session, path, ("op-rows",), after=pass_deadline)
        result = await _repository(session, clock=clock).purge_operation_spool_batch(cutoff=utcnow(), deadline=1500.0)

    assert (result.selected_operations, result.deleted_operations, result.budget_exhausted) == (1, 0, True)
    assert [commit.spool_rows_removed for commit in commits] == [0, _EVENT_LIMIT]
    assert _operation_rows(path, "op-rows") == 1
    assert _spool_rows(path, "op-rows") == _EVENT_ROWS - _EVENT_LIMIT


async def test_scheduled_retention_resumes_a_purge_its_time_budget_stopped(
    database: _Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory, path = database
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-rows", events=_EVENT_ROWS)

    clock = _SteppingClock(1.0)
    monkeypatch.setattr(cleanup_scheduler, "time", clock)
    monkeypatch.setattr(cleanup_scheduler, "_OPERATION_RETENTION_TIME_BUDGET_SECONDS", 3.0)
    passes: list[cleanup_scheduler.OperationRetentionCleanupResult] = []
    while _operation_rows(path, "op-rows") and len(passes) < 20:
        async with session_factory() as session:
            passes.append(
                await cleanup_scheduler._purge_operation_spool_with_budget(
                    _repository(session, clock=clock), cutoff=utcnow()
                )
            )

    assert len(passes) > 1
    assert all(result.outcome == "time_budget_exhausted" and result.backlog_likely for result in passes[:-1])
    assert passes[-1].outcome == "completed"
    assert sum(result.deleted_operations for result in passes) == 1
    assert _spool_rows(path, "op-rows") == 0


async def test_spool_purge_counts_a_committed_delete_before_a_cancelled_pause(database: _Database) -> None:
    session_factory, path = database
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-rows", events=3)

    pauses = _Pauses(cancel_when=lambda: _operation_rows(path, "op-rows") == 0)
    progress = DurableBridgeOperationPurgeProgress()
    session = session_factory()
    try:
        with pytest.raises(asyncio.CancelledError):
            await _repository(session, pauses=pauses).purge_operation_spool_batch(cutoff=utcnow(), progress=progress)
    finally:
        await session.close()

    assert progress.deleted_operations == 1
    assert _operation_rows(path, "op-rows") == 0


@pytest.mark.parametrize("cancelled", ["purge task", "commit task"])
async def test_spool_purge_counts_a_delete_whose_commit_cleanup_is_cancelled(
    database: _Database, cancelled: str
) -> None:
    session_factory, path = database
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-rows", events=3)

    progress = DurableBridgeOperationPurgeProgress()
    session = session_factory()
    purge_task: asyncio.Task[Any] | None = None

    def cancel_once_the_operation_row_is_gone(_session: object) -> None:
        # after_commit runs once the database commit is durable, while the
        # commit still has connection cleanup to await.
        if _operation_rows(path, "op-rows") == 0:
            target = purge_task if cancelled == "purge task" else asyncio.current_task()
            assert target is not None
            target.cancel()

    event.listen(session.sync_session, "after_commit", cancel_once_the_operation_row_is_gone)
    try:
        purge_task = asyncio.create_task(
            _repository(session).purge_operation_spool_batch(cutoff=utcnow(), progress=progress)
        )
        with pytest.raises(asyncio.CancelledError):
            await purge_task
    finally:
        await session.close()

    assert _operation_rows(path, "op-rows") == 0
    assert progress.deleted_operations == 1
    _assert_writer_slot_free(path)


async def test_spool_purge_counts_a_delete_whose_commit_is_cancelled_while_it_executes(database: _Database) -> None:
    session_factory, path = database
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-rows", events=3)

    loop = asyncio.get_running_loop()
    progress = DurableBridgeOperationPurgeProgress()
    session = session_factory()

    def cancel_during_commit(conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if not statement.startswith("DELETE FROM http_bridge_operations "):
            return
        purge = asyncio.current_task()
        assert purge is not None

        def trace(executed: str) -> None:
            # Runs on the aiosqlite worker thread while it executes COMMIT.
            if executed == "COMMIT":
                loop.call_soon_threadsafe(purge.cancel)

        conn.connection.dbapi_connection.run_async(lambda db: db.set_trace_callback(trace))

    event.listen(session.get_bind(), "after_cursor_execute", cancel_during_commit)
    try:
        with pytest.raises(asyncio.CancelledError):
            await _repository(session).purge_operation_spool_batch(cutoff=utcnow(), progress=progress)
    finally:
        await session.close()

    assert _operation_rows(path, "op-rows") == 0
    assert progress.deleted_operations == 1
    _assert_writer_slot_free(path)


async def test_spool_purge_checks_the_deadline_after_a_partial_delete(database: _Database) -> None:
    session_factory, path = database
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-rows", events=3)

    clock = _SteppingClock(0.0)

    def pass_deadline(commit_count: int) -> None:
        if commit_count == 2:
            clock.now = 2000.0

    async with session_factory() as session:
        commits = _observe_commits(session, path, ("op-rows",), after=pass_deadline)
        result = await _repository(session, clock=clock).purge_operation_spool_batch(cutoff=utcnow(), deadline=1500.0)

    assert (result.selected_operations, result.deleted_operations, result.budget_exhausted) == (1, 0, True)
    assert [commit.spool_rows_removed for commit in commits] == [0, 3]
    assert _operation_rows(path, "op-rows") == 1


async def test_spool_purge_skips_an_operation_without_a_session_row(database: _Database) -> None:
    session_factory, path = database
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-orphan", events=3)
    orphan = sqlite3.connect(path, isolation_level=None)
    try:
        # Only reachable with foreign keys off; the base never selected it.
        orphan.execute("PRAGMA foreign_keys=OFF")
        orphan.execute("DELETE FROM http_bridge_sessions WHERE id = 'session-op-orphan'")
    finally:
        orphan.close()

    async with session_factory() as session:
        result = await _repository(session).purge_operation_spool_batch(cutoff=utcnow())

    # Selecting a row that the recheck can never lock would report a full
    # batch forever and make the scheduler retry without delay.
    assert result.selected_operations == 0


async def test_spool_purge_stops_before_the_next_operation_at_the_deadline(database: _Database) -> None:
    session_factory, path = database
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-first", events=3)
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-second", events=3)

    clock = _SteppingClock(0.0)

    def pass_deadline(commit_count: int) -> None:
        # Commits: 1 selection, 2 events, 3 chunks, 4 the first operation row.
        if commit_count == 4:
            clock.now = 2000.0

    async with session_factory() as session:
        _observe_commits(session, path, ("op-first", "op-second"), after=pass_deadline)
        result = await _repository(session, clock=clock).purge_operation_spool_batch(cutoff=utcnow(), deadline=1500.0)

    assert (result.selected_operations, result.deleted_operations, result.budget_exhausted) == (2, 1, True)
    assert _operation_rows(path, "op-first") == 0
    # The second operation is left whole for the next pass.
    assert _spool_rows(path, "op-second") == 3


def _append_events(path: Path, operation_id: str, count: int) -> None:
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        writer.executemany(
            "INSERT INTO http_bridge_operation_events (event_id, operation_id, sequence_number, event_fingerprint,"
            " event_text, created_at) VALUES (?, ?, ?, ?, 'data: {}\n\n', CURRENT_TIMESTAMP)",
            [(f"late-{i}", operation_id, 100000 + i, f"late-fp-{i}") for i in range(count)],
        )
    finally:
        writer.close()


async def test_material_appended_before_the_operation_delete_is_removed_in_bounded_transactions(
    database: _Database,
) -> None:
    session_factory, path = database
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-rows", events=3)
    late_rows = _EVENT_LIMIT + 44
    after_operation_delete_attempt: list[tuple[int, int]] = []

    def late_append(commit_count: int) -> None:
        # Commits: 1 selection, 2 events, 3 chunks, 4 the operation delete.
        if commit_count == 3:
            _append_events(path, "op-rows", late_rows)
        if commit_count == 4:
            # _observe_commits cannot see cascades of rows appended after its
            # last snapshot, so sample the rows here.
            after_operation_delete_attempt.append((_operation_rows(path, "op-rows"), _spool_rows(path, "op-rows")))

    async with session_factory() as session:
        commits = _observe_commits(session, path, ("op-rows",), after=late_append)
        result = await _repository(session).purge_operation_spool_batch(cutoff=utcnow())

    # The operation delete found material and removed nothing, instead of
    # cascading it in one transaction; a further round removed it in bounded
    # transactions.
    assert after_operation_delete_attempt == [(1, late_rows)]
    assert max(commit.spool_rows_removed for commit in commits) <= _EVENT_LIMIT
    assert result.deleted_operations == 1
    assert _operation_rows(path, "op-rows") == 0
    assert _spool_rows(path, "op-rows") == 0


async def test_owner_reclaiming_between_the_recheck_and_the_delete_keeps_the_spool(database: _Database) -> None:
    session_factory, path = database
    async with session_factory() as session:
        await _seed_operation(session, operation_id="op-unknown", state="unknown", events=_EVENT_ROWS)
    reclaimed: list[bool] = []

    def reclaim_before_first_delete(_conn, _cursor, statement: str, _params, _context, _executemany) -> None:
        # Another process (outside the in-process writer gate) claims the
        # session after the recheck SELECT returned and before the DELETE.
        if statement.lstrip().startswith("DELETE FROM http_bridge_operation_events") and not reclaimed:
            reclaimed.append(True)
            _reclaim_session(path, "session-op-unknown", lease_seconds=300)

    bind = session_factory.kw["bind"]
    event.listen(bind.sync_engine, "before_cursor_execute", reclaim_before_first_delete)
    async with session_factory() as session:
        result = await _repository(session).purge_operation_spool_batch(cutoff=utcnow())

    assert reclaimed
    assert (result.selected_operations, result.deleted_operations) == (1, 0)
    assert _operation_rows(path, "op-unknown") == 1
    assert _spool_rows(path, "op-unknown") == _EVENT_ROWS


def _reclaim_session(path: Path, session_id: str, *, lease_seconds: int) -> None:
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        writer.execute(
            "UPDATE http_bridge_sessions SET owner_instance_id = 'instance', lease_expires_at = ? WHERE id = ?",
            ((utcnow() + timedelta(seconds=lease_seconds)).isoformat(sep=" "), session_id),
        )
    finally:
        writer.close()
