"""Access-log redaction of invite paths and credential query values.

Records are built the way uvicorn's access logger emits them: the message
template plus ``(client_addr, method, full_path, http_version, status_code)``
args, at INFO, so the keyed WARNING-level patterns do not run.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core.runtime_logging import JsonAccessFormatter, UtcAccessFormatter

pytestmark = pytest.mark.unit

# Assembled rather than written out: a long url-safe literal reads as a real
# secret to scanners.
_TOKEN = "invite-token-" + "0123456789abcdefghij" * 2
_SECRET = "value-" + "s3cr3t" * 4


def _access_record(full_path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.5:1234", "GET", full_path, "1.1", 200),
        None,
    )


def _render_both(full_path: str) -> tuple[str, str]:
    text = UtcAccessFormatter(fmt='%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False).format(
        _access_record(full_path)
    )
    request = json.loads(JsonAccessFormatter().format(_access_record(full_path)))["request"]
    return text, request


@pytest.mark.parametrize(
    ("full_path", "expected"),
    [
        (f"/invite/{_TOKEN}", "/invite/[REDACTED]"),
        (f"/api/dashboard-auth/invite/{_TOKEN}", "/api/dashboard-auth/invite/[REDACTED]"),
    ],
)
def test_browser_and_api_invite_paths_are_redacted(full_path: str, expected: str) -> None:
    for rendered in _render_both(full_path):
        assert _TOKEN not in rendered
        assert f"GET {expected} HTTP/1.1" in rendered


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (f"api_key={_SECRET}&model=gpt", "api_key=[REDACTED]&model=gpt"),
        (f"key={_SECRET}", "key=[REDACTED]"),
        (f"probe=1&access_token={_SECRET}", "probe=1&access_token=[REDACTED]"),
        (f"api%5Fkey={_SECRET}", "api%5Fkey=[REDACTED]"),
        (f"X-Api-Key={_SECRET}&client_secret={_SECRET}", "X-Api-Key=[REDACTED]&client_secret=[REDACTED]"),
        # ``;`` and quotes belong to the value for Starlette: the whole value goes.
        (f"api_key={_SECRET};TAIL&model=gpt", "api_key=[REDACTED]&model=gpt"),
        (f"api_key={_SECRET}'TAIL", "api_key=[REDACTED]"),
    ],
)
def test_credential_query_values_are_masked_at_info(query: str, expected: str) -> None:
    for rendered in _render_both(f"/v1/models?{query}"):
        assert _SECRET not in rendered
        assert "TAIL" not in rendered
        assert f"GET /v1/models?{expected} HTTP/1.1" in rendered


@pytest.mark.parametrize(
    "full_path",
    [
        "/health?probe=access-line",
        "/api/dashboard-auth/invite/accept",
        "/v1/models?monkey=1&author=me&keys=2",
        "/api/request-logs?limit=50&offset=0",
    ],
)
def test_ordinary_paths_and_parameters_stay_readable(full_path: str) -> None:
    for rendered in _render_both(full_path):
        assert f"GET {full_path} HTTP/1.1" in rendered
