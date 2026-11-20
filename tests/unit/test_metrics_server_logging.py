"""Building the metrics server config must not reconfigure process logging.

uvicorn applies ``log_config`` and ``log_level`` process wide when a
``Config`` is constructed, so this runs the real construction against the
server log configuration instead of starting a second listener.
"""

from __future__ import annotations

import logging
import logging.config
from collections.abc import Iterator

import pytest

from app.core.runtime_logging import build_log_config
from app.main import _metrics_server_config

pytestmark = pytest.mark.unit

_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access")


def _state(name: str) -> tuple[int, list[logging.Handler], bool, bool]:
    logger = logging.getLogger(name)
    return logger.level, list(logger.handlers), logger.propagate, logger.disabled


@pytest.fixture
def server_logging() -> Iterator[None]:
    saved = {name: _state(name) for name in _LOGGERS}
    logging.config.dictConfig(build_log_config())
    try:
        yield
    finally:
        for name, (level, handlers, propagate, disabled) in saved.items():
            logger = logging.getLogger(name)
            logger.setLevel(level)
            logger.handlers[:] = handlers
            logger.propagate = propagate
            logger.disabled = disabled


async def _metrics_app(scope: object, receive: object, send: object) -> None:
    del scope, receive, send


@pytest.mark.usefixtures("server_logging")
def test_metrics_server_config_keeps_server_logging() -> None:
    before = {name: _state(name) for name in _LOGGERS}

    config = _metrics_server_config(_metrics_app, 9)

    assert config.port == 9
    assert {name: _state(name) for name in _LOGGERS} == before
    assert logging.getLogger("uvicorn.access").isEnabledFor(logging.INFO)
