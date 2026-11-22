from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import app.db.session as session_module
from app.core.exceptions import DashboardRateLimitError
from app.core.rate_limiter.db_rate_limiter import DatabaseRateLimiter, get_rate_limit_attempt_sweeper
from app.db.models import Base, RateLimitAttempt
from tests.fixtures.sqlite_writer_section import record_writer_section_ownership

pytestmark = pytest.mark.unit


async def _check_then_record_failure(limiter: DatabaseRateLimiter, key: str, session: AsyncSession) -> None:
    """Mirror the login flow: refuse when the window is full, otherwise count the failure."""
    await limiter.check(key, session)
    await limiter.record_failure(key, session)


@pytest.fixture
async def async_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_single_instance_blocks_after_eight_attempts(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    limiter = DatabaseRateLimiter(max_attempts=8, window_seconds=60, type="totp")

    async with async_session_factory() as session:
        for _ in range(8):
            await _check_then_record_failure(limiter, "ip:single", session)

        with pytest.raises(DashboardRateLimitError):
            await _check_then_record_failure(limiter, "ip:single", session)


@pytest.mark.asyncio
async def test_cross_replica_combined_attempts_are_enforced(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    replica_one = DatabaseRateLimiter(max_attempts=8, window_seconds=60, type="password")
    replica_two = DatabaseRateLimiter(max_attempts=8, window_seconds=60, type="password")

    async with async_session_factory() as session_one:
        for _ in range(4):
            await _check_then_record_failure(replica_one, "ip:replica", session_one)

    async with async_session_factory() as session_two:
        for _ in range(4):
            await _check_then_record_failure(replica_two, "ip:replica", session_two)

    async with async_session_factory() as session_one_again:
        with pytest.raises(DashboardRateLimitError):
            await _check_then_record_failure(replica_one, "ip:replica", session_one_again)


@pytest.mark.asyncio
async def test_window_expiry_ignores_old_attempts(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    limiter = DatabaseRateLimiter(max_attempts=1, window_seconds=60, type="totp")

    async with async_session_factory() as session:
        old_attempt = RateLimitAttempt(
            key="ip:expired",
            type="totp",
            attempted_at=datetime.now(UTC) - timedelta(minutes=2),
        )
        session.add(old_attempt)
        await session.commit()

        await _check_then_record_failure(limiter, "ip:expired", session)
        with pytest.raises(DashboardRateLimitError):
            await _check_then_record_failure(limiter, "ip:expired", session)


def test_migration_upgrade_downgrade_upgrade_is_reversible(tmp_path: Path) -> None:
    db_path = tmp_path / "rate_limit_migration.db"
    cfg = Config()
    cfg.set_main_option("script_location", str((Path(__file__).resolve().parents[2] / "app/db/alembic").resolve()))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}", future=True)
    try:
        inspector = sa.inspect(engine)
        assert inspector.has_table("rate_limit_attempts") is True
    finally:
        engine.dispose()


# Regression tests guarding the contract that successful logins must not
# count toward lockout. The previous implementation incremented the
# attempts counter on every check(), so eight successful logins would
# trip the same lockout threshold as eight failures.


@pytest.mark.asyncio
async def test_successful_login_not_counted_toward_lockout(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """8 successful logins must NOT cause lockout; only failures count."""
    limiter = DatabaseRateLimiter(max_attempts=8, window_seconds=60, type="password")

    async with async_session_factory() as session:
        # Simulate 8 successful logins: check (no record) then clear on success
        for _ in range(8):
            await limiter.check("ip:success-test", session)
            await limiter.clear_for_key("ip:success-test", session)

        # A 9th successful login must still be allowed — counter stays at 0
        await limiter.check("ip:success-test", session)


@pytest.mark.asyncio
async def test_clear_for_key_resets_lockout(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Successful login should reset the rate limit counter via clear_for_key."""
    limiter = DatabaseRateLimiter(max_attempts=8, window_seconds=60, type="password")

    async with async_session_factory() as session:
        # Record 8 failed attempts
        for _ in range(8):
            await _check_then_record_failure(limiter, "ip:clear-test", session)

        # clear_for_key (defined in app/core/rate_limiter/db_rate_limiter.py)
        # resets the lockout window for the given key.
        await limiter.clear_for_key("ip:clear-test", session)

        # After clearing, should be able to attempt again
        await _check_then_record_failure(limiter, "ip:clear-test", session)


@pytest.mark.asyncio
async def test_check_only_does_not_increment_counter(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """check() should only read, not write to the attempts table."""
    limiter = DatabaseRateLimiter(max_attempts=2, window_seconds=60, type="password")

    async with async_session_factory() as session:
        # check() without recording — 10 checks should NOT block
        for _ in range(10):
            await limiter.check("ip:check-only", session)

        # Still able to record
        await _check_then_record_failure(limiter, "ip:check-only", session)


@pytest.mark.asyncio
async def test_record_failure_only_counts_failures(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """record_failure() should be the method called only when authentication fails."""
    limiter = DatabaseRateLimiter(max_attempts=3, window_seconds=60, type="password")

    async with async_session_factory() as session:
        # 2 failures recorded explicitly
        await limiter.record_failure("ip:failure-test", session)
        await limiter.record_failure("ip:failure-test", session)

        # 1 success (clear) in between
        await limiter.clear_for_key("ip:failure-test", session)

        # 1 more failure after success — should only be 1 total
        await limiter.record_failure("ip:failure-test", session)

        # Should NOT be blocked (only 1 failure since last success)
        await limiter.check("ip:failure-test", session)


@dataclass(frozen=True, slots=True)
class _FileDatabaseSettings:
    database_url: str


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["check_and_increment", "record_failure"])
async def test_the_window_is_measured_after_waiting_for_the_writer_section(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    """Attempts that expire while the write waits for the section no longer count."""
    monkeypatch.setattr(
        session_module, "_settings", _FileDatabaseSettings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    )
    monkeypatch.setattr(session_module, "_sqlite_writer_lock", None)
    limiter = DatabaseRateLimiter(max_attempts=8, window_seconds=60, type="password")
    almost_expired = datetime.now(UTC) - timedelta(seconds=59)
    async with async_session_factory() as session:
        session.add_all(
            [RateLimitAttempt(key="ip:queued", type="password", attempted_at=almost_expired) for _ in range(8)]
        )
        await session.commit()

    holding = asyncio.Event()

    async def hold_the_section() -> None:
        async with session_module.sqlite_writer_section():
            holding.set()
            await asyncio.sleep(1.1)

    holder = asyncio.create_task(hold_the_section())
    await holding.wait()
    async with async_session_factory() as session:
        await getattr(limiter, operation)("ip:queued", session)
    await holder


@pytest.mark.asyncio
async def test_the_retention_sweep_runs_inside_the_writer_section(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        session_module, "_settings", _FileDatabaseSettings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    )
    monkeypatch.setattr(session_module, "_sqlite_writer_lock", None)
    async with async_session_factory() as session:
        engine = cast(AsyncEngine, session.bind)
        with record_writer_section_ownership(engine.sync_engine) as recorded:
            await get_rate_limit_attempt_sweeper().cleanup(session)

    assert recorded.held_for("DELETE FROM rate_limit_attempts") == [True]
