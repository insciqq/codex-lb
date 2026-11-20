from __future__ import annotations

import logging
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from time import monotonic

from alembic.runtime.migration import MigrationContext, RevisionStep

logger = logging.getLogger(__name__)

# Alembic's private hook holding the revision-step generator for this run.
_MIGRATIONS_FN_ATTR = "_migrations_fn"


@contextmanager
def revision_progress(context: MigrationContext) -> Generator[None]:
    """Report online revision execution while Alembic retains transaction control."""
    # Alembic exposes only an after-apply callback. Keep the iterator adapter
    # here so starts and failures can be reported without replacing its runner.
    # The hook is private: if a future Alembic renames it or runs without one,
    # migrate without progress records rather than failing the migration.
    original: Callable[..., Iterator[object]] | None = getattr(context, _MIGRATIONS_FN_ATTR, None)
    if not callable(original):
        logger.warning("Migration progress unavailable: Alembic runner hook not found")
        yield
        return
    active: RevisionStep | None = None
    started = 0.0

    def steps(heads: tuple[str, ...], migration_context: MigrationContext) -> Iterator[object]:
        nonlocal active, started
        for step in original(heads, migration_context):
            if not isinstance(step, RevisionStep):
                yield step
                continue
            active = step
            started = monotonic()
            direction = "upgrade" if step.is_upgrade else "downgrade"
            logger.info("Migration started revision=%s direction=%s", step.revision.revision, direction)
            yield step
            logger.info(
                "Migration executed revision=%s direction=%s elapsed_seconds=%.3f",
                step.revision.revision,
                direction,
                monotonic() - started,
            )
            active = None

    setattr(context, _MIGRATIONS_FN_ATTR, steps)
    try:
        yield
    except BaseException:
        if active is not None:
            logger.error(
                "Migration failed revision=%s direction=%s elapsed_seconds=%.3f",
                active.revision.revision,
                "upgrade" if active.is_upgrade else "downgrade",
                monotonic() - started,
            )
        raise
    finally:
        setattr(context, _MIGRATIONS_FN_ATTR, original)
