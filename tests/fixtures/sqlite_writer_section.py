"""Hold the in-process SQLite writer section, and check who holds it at each write."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field

import pytest
from sqlalchemy import Engine, event, text

import app.db.session as session_module
from app.db.session import get_background_session


def skip_unless_file_backed_sqlite() -> None:
    """Skip a test of the writer section when the suite does not run on file-backed SQLite."""
    if not session_module._is_sqlite_url(session_module._database_url) or session_module._is_sqlite_memory_url(
        session_module._database_url
    ):
        pytest.skip("the writer section is only active on file-backed SQLite")


@asynccontextmanager
async def section_writer_holding_the_slot(
    monkeypatch: pytest.MonkeyPatch, *, hold_seconds: float
) -> AsyncGenerator[None]:
    """Hold the in-process SQLite writer section and the writer slot past the busy timeout.

    Stands in for the queue of request-path writers that serialize through the
    section and hand the slot to each other back to back. The busy timeout is
    lowered for new connections so the test does not wait out 30 s.
    """

    skip_unless_file_backed_sqlite()
    monkeypatch.setattr(session_module, "_SQLITE_BUSY_TIMEOUT_MS", 200)
    holding = asyncio.Event()

    async def hold() -> None:
        async with session_module.sqlite_writer_section():
            async with get_background_session() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                holding.set()
                await asyncio.sleep(hold_seconds)
                await session.rollback()

    holder = asyncio.create_task(hold())
    await asyncio.wait_for(holding.wait(), timeout=5)
    try:
        yield
    finally:
        await holder


@dataclass
class WriterSectionWrites:
    """Write statements seen on an engine, each with whether the writer section was held."""

    writes: list[tuple[str, bool]] = field(default_factory=list)

    def held_for(self, prefix: str) -> list[bool]:
        """Whether the section was held, for every write starting with ``prefix`` (case-insensitive)."""
        return [held for statement, held in self.writes if statement.upper().startswith(prefix.upper())]

    def held_containing(self, fragment: str) -> list[bool]:
        """Whether the section was held, for every write whose SQL contains ``fragment``."""
        return [held for statement, held in self.writes if fragment in statement]


@contextmanager
def record_writer_section_ownership(*engines: Engine) -> Generator[WriterSectionWrites]:
    """Record, for every INSERT/UPDATE/DELETE, whether the writer section lock was held.

    Only meaningful while nothing else holds the section, so a held lock at a
    write means the writing request holds it.
    """

    recorded = WriterSectionWrites()

    def record(_conn, _cursor, statement: str, _params, _context, _executemany) -> None:
        normalized = " ".join(statement.split())
        if not normalized.upper().startswith(("INSERT", "UPDATE", "DELETE")):
            return
        lock = session_module._sqlite_writer_lock
        recorded.writes.append((normalized, lock is not None and lock.locked()))

    for engine in engines:
        event.listen(engine, "before_cursor_execute", record)
    try:
        yield recorded
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", record)


def app_sync_engines() -> tuple[Engine, ...]:
    """The main engine and, when started, the background engine of the app."""
    engines = [session_module.engine.sync_engine]
    background = session_module._background_engine
    if background is not None and background.sync_engine is not engines[0]:
        engines.append(background.sync_engine)
    return tuple(engines)
