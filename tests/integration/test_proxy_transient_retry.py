"""Tests for transient stream retry logic.

Covers:
- Streaming: SSE-level transient errors → same-account retry → failover
- Streaming: HTTP-level 500 → same-account retry → failover
- Compact: HTTP 500 → same-account retry with backoff → account failover
- Budget exhaustion during inner retry
- Non-500 errors are not intercepted by transient retry
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp.client_reqrep import ConnectionKey
from sqlalchemy import select

import app.core.clients.proxy as core_proxy
import app.core.resilience.network_recovery as network_recovery
import app.modules.proxy.account_cache as account_cache_module
import app.modules.proxy.api as proxy_api_module
import app.modules.proxy.service as proxy_module
from app.core.auth import generate_unique_account_id
from app.core.clients.proxy import ProxyResponseError
from app.core.clock import RealScheduler
from app.core.errors import openai_error
from app.core.openai.models import CompactResponsePayload
from app.core.usage.models import RateLimitPayload, UsagePayload, UsageWindow
from app.core.utils.request_id import get_request_id
from app.db.models import Account, AccountStatus, ApiKeyUsageReservation
from app.db.session import SessionLocal
from app.modules.proxy._service import observability as proxy_observability_module
from app.modules.proxy.account_cache import get_account_selection_cache
from app.modules.usage import updater as usage_updater_module
from app.modules.usage.repository import UsageRepository
from tests.unit._proxy_test_helpers import WindowsOSError

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _force_usage_weighted_routing(async_client) -> None:
    current = await async_client.get("/api/settings")
    assert current.status_code == 200
    payload = current.json()
    payload["routingStrategy"] = "usage_weighted"
    response = await async_client.put("/api/settings", json=payload)
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encode_jwt(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"header.{body}.sig"


def _make_auth_json(account_id: str, email: str) -> dict:
    payload = {
        "email": email,
        "chatgpt_account_id": account_id,
        "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
    }
    return {
        "tokens": {
            "idToken": _encode_jwt(payload),
            "accessToken": "access-token",
            "refreshToken": "refresh-token",
            "accountId": account_id,
        },
    }


async def _import_account(async_client, account_id: str, email: str) -> str:
    auth_json = _make_auth_json(account_id, email)
    files = {"auth_json": ("auth.json", json.dumps(auth_json), "application/json")}
    response = await async_client.post("/api/accounts/import", files=files)
    assert response.status_code == 200
    return generate_unique_account_id(account_id, email)


def _sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _server_error_sse_event() -> str:
    return _sse_event(
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "code": "server_error",
                    "message": "An error occurred while processing your request.",
                },
            },
        }
    )


def _overload_sse_event(error_code: str) -> str:
    return _sse_event(
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "code": error_code,
                    "message": "Our servers are currently overloaded. Please try again later.",
                },
            },
        }
    )


def _stream_timeout_sse_event() -> str:
    return _sse_event(
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "code": "upstream_request_timeout",
                    "message": "Proxy request budget exhausted",
                },
            },
        }
    )


def _model_capacity_sse_event() -> str:
    return _sse_event(
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "code": "invalid_request_error",
                    "message": "Selected model is at capacity. Please try a different model.",
                },
            },
        }
    )


def _success_sse_event(response_id: str = "resp_ok") -> str:
    return _sse_event(
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }
    )


def _extract_events(lines: list[str]) -> list[dict]:
    events = []
    for line in lines:
        if line.startswith("data: "):
            data = line[6:]
            if data == "[DONE]":
                continue
            events.append(json.loads(data))
    return events


# ===========================================================================
# Streaming — SSE-level server_error
# ===========================================================================


@pytest.mark.asyncio
async def test_stream_server_error_surfaces_without_replay(async_client, monkeypatch):
    """An upstream terminal server error cannot prove that retrying the POST is safe."""
    await _import_account(async_client, "acc_trans_1", "trans1@example.com")

    call_count = 0
    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        nonlocal call_count
        call_count += 1
        seen_account_ids.append(account_id)
        if call_count == 1:
            yield _server_error_sse_event()
            return
        yield _success_sse_event()

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    failed = [event for event in events if event.get("type") == "response.failed"]
    assert len(failed) == 1
    assert failed[0]["response"]["error"]["code"] == "server_error"
    assert seen_account_ids == ["acc_trans_1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["overloaded_error", "server_is_overloaded"])
async def test_stream_overload_alias_surfaces_without_replay(async_client, monkeypatch, error_code):
    await _import_account(async_client, f"acc_{error_code}", f"{error_code}@example.com")

    call_count = 0
    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        nonlocal call_count
        call_count += 1
        seen_account_ids.append(account_id)
        if call_count == 1:
            yield _overload_sse_event(error_code)
            return
        yield _success_sse_event("resp_server_overloaded_ok")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    failed = [event for event in events if event.get("type") == "response.failed"]
    assert len(failed) == 1
    assert failed[0]["response"]["error"]["code"] == error_code
    assert seen_account_ids == [f"acc_{error_code}"]


@pytest.mark.asyncio
async def test_stream_timeout_surfaces_without_replay(async_client, monkeypatch):
    """An upstream terminal timeout is not proven pre-dispatch work."""
    await _import_account(async_client, "acc_trans_timeout", "timeout@example.com")

    call_count = 0
    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        nonlocal call_count
        call_count += 1
        seen_account_ids.append(account_id)
        if call_count == 1:
            yield _stream_timeout_sse_event()
            return
        yield _success_sse_event()

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    failed = [e for e in events if e.get("type") == "response.failed"]
    assert len(failed) == 1
    assert failed[0]["response"]["error"]["code"] == "upstream_request_timeout"
    assert seen_account_ids == ["acc_trans_timeout"]


@pytest.mark.asyncio
async def test_stream_model_capacity_without_response_id_surfaces_without_replay(async_client, monkeypatch):
    """An upstream terminal event is accepted work even when it omits a response id."""
    await _import_account(async_client, "acc_model_capacity_retry", "model-capacity@example.com")

    call_count = 0
    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        nonlocal call_count
        call_count += 1
        seen_account_ids.append(account_id)
        if call_count == 1:
            yield _model_capacity_sse_event()
            return

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    failed = [event for event in events if event.get("type") == "response.failed"]
    assert len(failed) == 1
    assert failed[0]["response"]["error"]["code"] == "invalid_request_error"
    assert seen_account_ids == ["acc_model_capacity_retry"]


@pytest.mark.asyncio
async def test_stream_raw_capacity_error_with_proxy_request_id_surfaces_without_replay(async_client, monkeypatch):
    """A terminal upstream error remains terminal even with a proxy-generated id."""
    await _import_account(async_client, "acc_raw_model_capacity_retry", "raw-model-capacity@example.com")

    call_count = 0
    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        nonlocal call_count
        call_count += 1
        seen_account_ids.append(account_id)
        if call_count == 1:
            yield _sse_event(
                {
                    "type": "error",
                    "code": "invalid_request_error",
                    "message": "Selected model is at capacity. Please try a different model.",
                }
            )
            return

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    errors = [event for event in events if event.get("type") == "error"]
    assert len(errors) == 1
    assert errors[0]["code"] == "invalid_request_error"
    assert seen_account_ids == ["acc_raw_model_capacity_retry"]


@pytest.mark.asyncio
async def test_stream_model_capacity_top_level_response_id_surfaces_without_replay(async_client, monkeypatch):
    """A top-level upstream response_id proves dispatch, so capacity errors must not be replayed."""
    await _import_account(async_client, "acc_model_capacity_accepted", "model-capacity-accepted@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        yield _sse_event(
            {
                "type": "response.failed",
                "response_id": "resp_model_capacity_accepted",
                "response": {
                    "error": {
                        "code": "invalid_request_error",
                        "message": "Selected model is at capacity. Please try a different model.",
                    },
                },
            }
        )

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    failed = [event for event in events if event.get("type") == "response.failed"]
    assert len(failed) == 1
    assert failed[0]["response"]["error"]["code"] == "invalid_request_error"
    assert seen_account_ids == ["acc_model_capacity_accepted"]


_USAGE_LIMIT_FRAME_ERRORS = (
    # The code-less form, which normalizes to ``upstream_error``.
    {"message": "The usage limit has been reached"},
    # The same rejection in the second-person wording, still code-less.
    {"message": "You've hit your usage limit."},
    # The coded form, which the code table already answered for.
    {"code": "usage_limit_reached", "message": "The usage limit has been reached"},
)


@pytest.mark.asyncio
@pytest.mark.parametrize("frame_error", _USAGE_LIMIT_FRAME_ERRORS)
async def test_stream_serialized_usage_limit_frame_without_a_code_walks_the_pool(
    async_client, monkeypatch, frame_error
):
    """Upstream sends the usage-limit rejection as a frame as well as a body, and the walk
    must not depend on which one arrived. The serialized form carries no status, and the code it
    does or does not carry is in no transport retry list, so the sentence is the only evidence
    there is -- reading it is what keeps the two forms on one answer instead of surfacing the
    first account's error while the body form rotates."""
    account_a_id = await _import_account(async_client, "acc_stream_frame_limit_a", "streamframelimita@example.com")
    await _import_account(async_client, "acc_stream_frame_limit_b", "streamframelimitb@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if account_id == "acc_stream_frame_limit_a":
            yield _sse_event({"type": "response.failed", "response": {"error": frame_error}})
            return
        yield _success_sse_event("resp_stream_frame_limit_ok")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    assert [event for event in events if event.get("type") == "response.failed"] == []
    assert len([event for event in events if event.get("type") == "response.completed"]) == 1
    assert seen_account_ids[:2] == ["acc_stream_frame_limit_a", "acc_stream_frame_limit_b"]

    async with SessionLocal() as session:
        exhausted_account = await session.get(Account, account_a_id)
        assert exhausted_account is not None
        assert exhausted_account.status == AccountStatus.RATE_LIMITED


@pytest.mark.asyncio
@pytest.mark.parametrize("frame_error", _USAGE_LIMIT_FRAME_ERRORS)
async def test_stream_usage_limit_frame_on_the_last_account_still_benches_it(async_client, monkeypatch, frame_error):
    """The account the walk ends on is the one the next request will be handed first.

    Every account rejects, so the last one is reached with no candidate left to move to and its
    frame is terminal rather than retried. Its health still has to be recorded, or the account
    that most recently said its window is spent is the one selection likes best -- and whether
    upstream attached the error code decides nothing about what the account can serve.
    """
    account_ids = [
        await _import_account(async_client, f"acc_stream_last_limit_{letter}", f"streamlastlimit{letter}@example.com")
        for letter in ("a", "b", "c")
    ]

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        yield _sse_event({"type": "response.failed", "response": {"error": frame_error}})

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        [line async for line in resp.aiter_lines() if line]

    async with SessionLocal() as session:
        for account_id in account_ids:
            account = await session.get(Account, account_id)
            assert account is not None
            assert account.status == AccountStatus.RATE_LIMITED


@pytest.mark.asyncio
@pytest.mark.parametrize("frame_error", _USAGE_LIMIT_FRAME_ERRORS)
async def test_stream_usage_limit_frame_after_a_visible_event_still_benches_the_account(
    async_client, monkeypatch, frame_error
):
    """Downstream visibility forbids moving this request, not recording what upstream said.

    The rejection arrives once a lifecycle event has already been relayed, so the frame is
    surfaced as-is. That is the whole remedy available to *this* request; the account's health is
    what protects the next one, and it must not depend on the error code being present.
    """
    account_id = await _import_account(async_client, "acc_stream_visible_limit", "streamvisiblelimit@example.com")

    async def fake_stream(payload, headers, access_token, account_id_arg, base_url=None, raise_for_status=False):
        yield _sse_event({"type": "response.created", "response": {"id": "resp_stream_visible_limit"}})
        yield _sse_event({"type": "response.failed", "response": {"error": frame_error}})

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    assert len([event for event in events if event.get("type") == "response.failed"]) == 1

    async with SessionLocal() as session:
        account = await session.get(Account, account_id)
        assert account is not None
        assert account.status == AccountStatus.RATE_LIMITED


@pytest.mark.asyncio
async def test_stream_empty_upstream_body_surfaces_without_replay(async_client, monkeypatch):
    """An untyped empty upstream stream may be post-dispatch, so it is not replayed."""
    await _import_account(async_client, "acc_empty_body_no_replay", "empty-body-no-replay@example.com")

    call_count = 0
    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        nonlocal call_count
        call_count += 1
        seen_account_ids.append(account_id)
        if call_count == 1:
            if False:
                yield ""
            return

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    error_codes = [event["response"]["error"]["code"] for event in events if event.get("type") == "response.failed"]
    assert error_codes[-1] == "stream_incomplete"
    assert seen_account_ids == ["acc_empty_body_no_replay"]


@pytest.mark.asyncio
async def test_stream_body_read_client_error_surfaces_without_replay(async_client, monkeypatch):
    """Post-connect body-read errors are not replayed because upstream delivery is uncertain."""
    await _import_account(async_client, "acc_previsible_disconnect_a", "previsible-disconnect-a@example.com")
    await _import_account(async_client, "acc_previsible_disconnect_b", "previsible-disconnect-b@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if False:
            yield ""
        raise aiohttp.ServerDisconnectedError("Server disconnected")

    async def fake_sleep(delay: float, result: None = None) -> None:
        pass

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)
    monkeypatch.setattr(proxy_module.asyncio, "sleep", fake_sleep)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    error_codes = [event["response"]["error"]["code"] for event in events if event.get("type") == "response.failed"]
    assert error_codes[-1] == "upstream_unavailable"
    assert "no_accounts" not in error_codes
    assert seen_account_ids == ["acc_previsible_disconnect_a"]


@pytest.mark.asyncio
async def test_stream_serialized_body_read_disconnect_with_response_id_surfaces_without_replay(
    async_client, monkeypatch
):
    """Serialized post-dispatch body-read failures with response_id are not safe to replay."""
    await _import_account(async_client, "acc_serialized_disconnect_a", "serialized-disconnect-a@example.com")
    await _import_account(async_client, "acc_serialized_disconnect_b", "serialized-disconnect-b@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        yield _sse_event(
            {
                "type": "response.failed",
                "response_id": "resp_serialized_disconnect",
                "response": {
                    "error": {
                        "code": "upstream_unavailable",
                        "message": "Server disconnected",
                    },
                },
            }
        )

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    failed = [event for event in events if event.get("type") == "response.failed"]
    assert len(failed) == 1
    assert failed[0]["response"]["error"]["code"] == "upstream_unavailable"
    assert seen_account_ids == ["acc_serialized_disconnect_a"]


@pytest.mark.asyncio
async def test_stream_serialized_body_read_disconnect_with_request_id_surfaces_without_replay(
    async_client, monkeypatch
):
    """A response.failed using the proxy request id still represents an unsafe post-dispatch failure."""
    await _import_account(async_client, "acc_serialized_request_id_disconnect_a", "serialized-request-a@example.com")
    await _import_account(async_client, "acc_serialized_request_id_disconnect_b", "serialized-request-b@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        del payload, headers, access_token, base_url, raise_for_status
        seen_account_ids.append(account_id)
        request_id = get_request_id()
        assert request_id is not None
        yield _sse_event(
            {
                "type": "response.failed",
                "response": {
                    "id": request_id,
                    "error": {
                        "code": "upstream_unavailable",
                        "message": "Server disconnected",
                    },
                },
            }
        )

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    failed = [event for event in events if event.get("type") == "response.failed"]
    assert len(failed) == 1
    assert failed[0]["response"]["error"]["code"] == "upstream_unavailable"
    assert seen_account_ids == ["acc_serialized_request_id_disconnect_a"]


@pytest.mark.asyncio
async def test_stream_pinned_previsible_close_exhaustion_surfaces_stream_incomplete(async_client, monkeypatch):
    """Pinned previous-response EOF cannot fail over, so preserve the stream failure for the client."""
    upstream_account_id = "acc_pinned_previsible_close"
    owner_account_id = await _import_account(
        async_client,
        upstream_account_id,
        "pinned-previsible-close@example.com",
    )

    seen_account_ids: list[str | None] = []

    async def fake_owner(self, *, previous_response_id, api_key, session_id=None, surface):
        del self, previous_response_id, api_key, session_id, surface
        return owner_account_id

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if False:
            yield ""
        return

    async def fake_sleep(delay: float, result: None = None) -> None:
        pass

    monkeypatch.setattr(proxy_module.ProxyService, "_resolve_websocket_previous_response_owner", fake_owner)
    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)
    monkeypatch.setattr(proxy_module.asyncio, "sleep", fake_sleep)

    payload = {
        "model": "gpt-5.1",
        "instructions": "hi",
        "input": [],
        "stream": True,
        "previous_response_id": "resp_pinned_previsible_close",
    }
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    error_codes = [event["response"]["error"]["code"] for event in events if event.get("type") == "response.failed"]
    assert error_codes[-1] == "stream_incomplete"
    assert "previous_response_owner_unavailable" not in error_codes
    assert set(seen_account_ids) == {upstream_account_id}


@pytest.mark.asyncio
async def test_stream_server_error_does_not_fail_over_after_accepted_terminal_event(async_client, monkeypatch):
    """A terminal event stays on its first account even when another account is eligible."""
    await _import_account(async_client, "acc_trans_fo_a", "fo_a@example.com")
    await _import_account(async_client, "acc_trans_fo_b", "fo_b@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if account_id == "acc_trans_fo_a":
            yield _server_error_sse_event()
            return
        yield _success_sse_event()

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    failed = [event for event in events if event.get("type") == "response.failed"]
    assert len(failed) == 1
    assert failed[0]["response"]["error"]["code"] == "server_error"
    assert seen_account_ids == ["acc_trans_fo_a"]


@pytest.mark.asyncio
async def test_stream_server_error_all_accounts_exhausted(async_client, monkeypatch):
    """server_error on all accounts → eventually returns error to client."""
    await _import_account(async_client, "acc_trans_all_a", "all_a@example.com")

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        yield _server_error_sse_event()

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    # Should end with an error event (either response.failed or no_accounts)
    last_event = events[-1] if events else {}
    assert last_event.get("type") in ("response.failed", "error")


@pytest.mark.asyncio
async def test_stream_server_error_does_not_make_a_third_post(async_client, monkeypatch):
    """An accepted terminal event prevents follow-up same-account POSTs."""
    await _import_account(async_client, "acc_trans_3rd", "trans3@example.com")

    call_count = 0
    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        nonlocal call_count
        call_count += 1
        seen_account_ids.append(account_id)
        if call_count <= 2:
            yield _server_error_sse_event()
            return
        yield _success_sse_event()

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    failed = [event for event in events if event.get("type") == "response.failed"]
    assert len(failed) == 1
    assert failed[0]["response"]["error"]["code"] == "server_error"
    assert seen_account_ids == ["acc_trans_3rd"]


# ===========================================================================
# Streaming — HTTP-level 500 (ProxyResponseError)
# ===========================================================================


@pytest.mark.asyncio
async def test_stream_http_500_retries_same_account_then_succeeds(async_client, monkeypatch):
    """HTTP 500 ProxyResponseError → inner retry on same account → success."""
    await _import_account(async_client, "acc_http500_1", "http500@example.com")

    call_count = 0
    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        nonlocal call_count
        call_count += 1
        seen_account_ids.append(account_id)
        if call_count == 1:
            raise ProxyResponseError(
                500,
                openai_error("server_error", "An error occurred while processing your request."),
                failure_phase="status",
            )
        yield _success_sse_event()

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    completed = [e for e in events if e.get("type") == "response.completed"]
    assert len(completed) == 1

    assert len(seen_account_ids) == 2
    assert seen_account_ids[0] == seen_account_ids[1]


@pytest.mark.asyncio
async def test_stream_http_500_exhausts_then_failover(async_client, monkeypatch):
    """HTTP 500 x3 on account A → failover to account B → success."""
    await _import_account(async_client, "acc_h5fo_a", "h5fo_a@example.com")
    await _import_account(async_client, "acc_h5fo_b", "h5fo_b@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if account_id == "acc_h5fo_a":
            raise ProxyResponseError(
                500,
                openai_error("server_error", "Internal server error"),
                failure_phase="status",
            )
        yield _success_sse_event()

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    completed = [e for e in events if e.get("type") == "response.completed"]
    assert len(completed) == 1

    a_calls = [aid for aid in seen_account_ids if aid == "acc_h5fo_a"]
    b_calls = [aid for aid in seen_account_ids if aid == "acc_h5fo_b"]
    assert len(a_calls) == 3
    assert len(b_calls) >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("winerror", [1231, 1232])
@pytest.mark.parametrize("pre_dispatch", [False, True])
async def test_stream_windows_route_recovery_preserves_replay_safety(async_client, monkeypatch, winerror, pre_dispatch):
    account_id = await _import_account(async_client, "acc_windows_transport", "windows-transport@example.com")
    error: OSError = WindowsOSError(winerror)
    if pre_dispatch:
        error = aiohttp.ClientConnectorError(ConnectionKey("example.invalid", 443, True, True, None, None, None), error)

    class FailedSession:
        def post(self, *args, **kwargs):
            del args, kwargs
            raise error

    failed_session = FailedSession()
    rotate = AsyncMock(return_value="rotated")
    monkeypatch.setattr(network_recovery, "rotate_shared_http_transport", rotate)
    monkeypatch.setattr(network_recovery, "backoff_seconds", lambda attempt: 0)
    calls: list[str | None] = []

    async def fake_stream(payload, headers, access_token, selected_account_id, **kwargs):
        del kwargs
        calls.append(selected_account_id)
        if len(calls) == 1:
            async for event in core_proxy.stream_responses(
                payload,
                headers,
                access_token,
                selected_account_id,
                session=cast(aiohttp.ClientSession, failed_session),
                upstream_stream_transport_override="http",
                raise_for_status=True,
            ):
                yield event
            return
        yield _success_sse_event("resp_windows_recovered")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)
    response = await async_client.post(
        "/backend-api/codex/responses",
        json={"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True},
    )

    rotate.assert_awaited_once()
    assert rotate.call_args.kwargs["failed_session"] is failed_session
    if pre_dispatch:
        assert "response.completed" in response.text, response.text
        assert calls == ["acc_windows_transport", "acc_windows_transport"]
    else:
        assert calls == ["acc_windows_transport"]
        assert "response.completed" not in response.text
        assert "proxy_network_unavailable" in response.text
        async with SessionLocal() as session:
            account = await session.get(Account, account_id)
            assert account is not None
            assert account.status == AccountStatus.ACTIVE


@pytest.mark.asyncio
@pytest.mark.parametrize("winerror", [64, 121])
async def test_stream_windows_reset_and_timeout_do_not_rotate_shared_transport(async_client, monkeypatch, winerror):
    await _import_account(async_client, "acc_windows_endpoint", "windows-endpoint@example.com")

    class FailedSession:
        def post(self, *args, **kwargs):
            del args, kwargs
            raise WindowsOSError(winerror)

    rotate = AsyncMock(return_value="rotated")
    monkeypatch.setattr(network_recovery, "rotate_shared_http_transport", rotate)
    monkeypatch.setattr(network_recovery, "backoff_seconds", lambda attempt: 0)

    async def fake_stream(payload, headers, access_token, selected_account_id, **kwargs):
        del kwargs
        async for event in core_proxy.stream_responses(
            payload,
            headers,
            access_token,
            selected_account_id,
            session=cast(aiohttp.ClientSession, FailedSession()),
            upstream_stream_transport_override="http",
            raise_for_status=True,
        ):
            yield event

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)
    response = await async_client.post(
        "/backend-api/codex/responses",
        json={"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True},
    )

    rotate.assert_not_awaited()
    assert "response.completed" not in response.text
    assert "proxy_network_unavailable" not in response.text
    assert "upstream_unavailable" in response.text, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/responses", "/backend-api/codex/responses"])
@pytest.mark.parametrize("owner_bound", [False, True])
@pytest.mark.parametrize("keyed", [False, True])
async def test_stream_deactivated_workspace_scopes_exclusion_and_failover(
    async_client,
    monkeypatch,
    path: str,
    owner_bound: bool,
    keyed: bool,
):
    account_a_id = await _import_account(async_client, "acc_workspace_a", "workspace-a@example.test")
    account_b_id = await _import_account(async_client, "acc_workspace_b", "workspace-b@example.test")
    seen_account_ids: list[str | None] = []
    headers = {}
    if keyed:
        settings_response = await async_client.put("/api/settings", json={"apiKeyAuthEnabled": True})
        assert settings_response.status_code == 200
        created = await async_client.post(
            "/api/api-keys/",
            json={
                "name": "workspace-failover",
                "limits": [
                    {"limitType": "total_tokens", "limitWindow": "weekly", "maxValue": 1_000_000},
                ],
            },
        )
        assert created.status_code == 200
        key_id = created.json()["id"]
        headers = {"authorization": f"Bearer {created.json()['key']}"}
        original_mark = proxy_module.LoadBalancer.mark_permanent_failure

        async def mark_after_settlement(self, account, code):
            async with SessionLocal() as session:
                pending = await session.scalar(
                    select(ApiKeyUsageReservation.id).where(
                        ApiKeyUsageReservation.api_key_id == key_id,
                        ApiKeyUsageReservation.status == "reserved",
                    )
                )
                assert pending is None
            return await original_mark(self, account, code)

        monkeypatch.setattr(proxy_module.LoadBalancer, "mark_permanent_failure", mark_after_settlement)

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if account_id == "acc_workspace_a":
            raise ProxyResponseError(
                402,
                openai_error("deactivated_workspace", "Workspace has been deactivated"),
                failure_phase="status",
            )
        yield _success_sse_event("resp_workspace_ok")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)
    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    if owner_bound:
        payload["input"] = [{"type": "reasoning", "id": "rs_workspace", "encrypted_content": "owner-bound"}]
    response = await async_client.post(path, json=payload, headers=headers)
    if owner_bound:
        assert seen_account_ids == ["acc_workspace_a"]
        if response.status_code == 402:
            assert response.json()["error"]["code"] == "deactivated_workspace"
        else:
            assert response.status_code == 200
            events = _extract_events(response.text.splitlines())
            assert [e["response"]["error"]["code"] for e in events if e.get("type") == "response.failed"] == [
                "deactivated_workspace"
            ]
    else:
        assert response.status_code == 200
        events = _extract_events(response.text.splitlines())
        assert [e["response"]["id"] for e in events if e.get("type") == "response.completed"] == ["resp_workspace_ok"]
        assert not [e for e in events if e.get("type") == "response.failed"]
        assert seen_account_ids == ["acc_workspace_a", "acc_workspace_b"]
        second = await async_client.post(path, json=payload, headers=headers)
        assert second.status_code == 200
        assert seen_account_ids == ["acc_workspace_a", "acc_workspace_b", "acc_workspace_b"]

    async with SessionLocal() as session:
        unavailable = await session.get(Account, account_a_id)
        healthy = await session.get(Account, account_b_id)
        assert unavailable is not None and healthy is not None
        assert unavailable.status == AccountStatus.DEACTIVATED
        assert unavailable.deactivation_reason == "Workspace has been deactivated"
        assert healthy.status == AccountStatus.ACTIVE
        assert healthy.deactivation_reason is None


@pytest.mark.asyncio
async def test_stream_bare_402_does_not_deactivate_account(async_client, monkeypatch):
    account_id = await _import_account(async_client, "acc_payment", "payment@example.test")

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        raise ProxyResponseError(402, openai_error("payment_required", "Payment Required"), failure_phase="status")
        yield ""  # pragma: no cover

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)
    response = await async_client.post("/v1/responses", json={"model": "gpt-5.1", "input": "hi", "stream": True})
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "payment_required"
    async with SessionLocal() as session:
        account = await session.get(Account, account_id)
        assert account is not None
        assert account.status == AccountStatus.ACTIVE
        assert account.deactivation_reason is None


@pytest.mark.asyncio
async def test_compact_deactivated_workspace_fails_over_once(async_client, monkeypatch):
    account_a_id = await _import_account(async_client, "acc_workspace_compact_a", "compact-a@example.test")
    account_b_id = await _import_account(async_client, "acc_workspace_compact_b", "compact-b@example.test")
    seen_account_ids: list[str | None] = []

    async def fake_compact(payload, headers, access_token, account_id):
        seen_account_ids.append(account_id)
        if account_id == "acc_workspace_compact_a":
            raise ProxyResponseError(
                402,
                openai_error("deactivated_workspace", "Workspace has been deactivated"),
                failure_phase="status",
            )
        return CompactResponsePayload.model_validate({"object": "response.compaction", "output": []})

    monkeypatch.setattr(proxy_module, "core_compact_responses", fake_compact)
    response = await async_client.post(
        "/backend-api/codex/responses/compact",
        json={"model": "gpt-5.1", "instructions": "hi", "input": []},
    )
    assert response.status_code == 200
    assert response.json()["object"] == "response.compaction"
    assert seen_account_ids == ["acc_workspace_compact_a", "acc_workspace_compact_b"]
    async with SessionLocal() as session:
        unavailable = await session.get(Account, account_a_id)
        healthy = await session.get(Account, account_b_id)
        assert unavailable is not None and healthy is not None
        assert unavailable.status == AccountStatus.DEACTIVATED
        assert unavailable.deactivation_reason == "Workspace has been deactivated"
        assert healthy.status == AccountStatus.ACTIVE


@pytest.mark.asyncio
@pytest.mark.parametrize("frame_error", _USAGE_LIMIT_FRAME_ERRORS)
async def test_stream_connect_phase_429_usage_limit_transparent_failover(async_client, monkeypatch, frame_error):
    """Connect-phase 429/usage_limit_reached on A should fail over to B before any downstream event."""
    account_a_id = await _import_account(async_client, "acc_stream_429_a", "stream429a@example.com")
    await _import_account(async_client, "acc_stream_429_b", "stream429b@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if account_id == "acc_stream_429_a":
            raise ProxyResponseError(
                429,
                {"error": frame_error},
                failure_phase="status",
            )
        yield _success_sse_event("resp_stream_429_ok")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    completed = [e for e in events if e.get("type") == "response.completed"]
    failed = [e for e in events if e.get("type") == "response.failed"]
    assert len(completed) == 1
    assert len(failed) == 0
    assert seen_account_ids[:2] == ["acc_stream_429_a", "acc_stream_429_b"]

    async with SessionLocal() as session:
        exhausted_account = await session.get(Account, account_a_id)
        assert exhausted_account is not None
        assert exhausted_account.status == AccountStatus.RATE_LIMITED


@pytest.mark.asyncio
@pytest.mark.parametrize("frame_error", _USAGE_LIMIT_FRAME_ERRORS)
async def test_stream_compacted_input_429_usage_limit_transparent_failover(async_client, monkeypatch, frame_error):
    """A pre-visible 429 must not bind retained input to the rejected account."""
    await _import_account(async_client, "acc_stream_compact_429_a", "streamcompact429a@example.com")
    await _import_account(async_client, "acc_stream_compact_429_b", "streamcompact429b@example.com")

    seen_account_ids: list[str | None] = []
    rejected_account_id: str | None = None

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        nonlocal rejected_account_id
        seen_account_ids.append(account_id)
        if rejected_account_id is None:
            rejected_account_id = account_id
            raise ProxyResponseError(
                429,
                {"error": frame_error},
                failure_phase="status",
            )
        assert account_id != rejected_account_id
        yield _success_sse_event("resp_stream_compact_429_ok")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.1",
        "instructions": "describe the image",
        "input": [
            {"type": "compaction", "encrypted_content": "opaque-compacted-history"},
            {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo="},
        ],
        "prompt_cache_key": "cache_compacted_image_429",
        "stream": True,
    }
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    completed = [e for e in events if e.get("type") == "response.completed"]
    failed = [e for e in events if e.get("type") == "response.failed"]
    assert len(completed) == 1
    assert len(failed) == 0
    assert len(seen_account_ids) == 2
    assert seen_account_ids[0] != seen_account_ids[1]


@pytest.mark.asyncio
async def test_stream_unresolved_file_reference_429_remains_on_first_dispatch_account(async_client, monkeypatch):
    """An expired file pin still relies on transient payload ownership and must not cross accounts."""
    await _import_account(async_client, "acc_stream_file_429_a", "streamfile429a@example.com")
    await _import_account(async_client, "acc_stream_file_429_b", "streamfile429b@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        raise ProxyResponseError(
            429,
            openai_error("usage_limit_reached", "usage limit reached"),
            failure_phase="status",
        )
        yield  # pragma: no cover - retain the async-generator transport contract

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    response = await async_client.post(
        "/backend-api/codex/responses",
        json={
            "model": "gpt-5.1",
            "input": [{"type": "input_file", "file_id": "file_expired_owner_pin"}],
            "prompt_cache_key": "cache_expired_file_429",
            "stream": True,
        },
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "usage_limit_reached"
    assert len(seen_account_ids) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "input_items",
    [
        pytest.param(
            [
                {
                    "type": "function_call",
                    "call_id": "call_program_owned",
                    "name": "lookup",
                    "arguments": "{}",
                    "caller": {"type": "program", "caller_id": "prog_owner_bound"},
                    "encrypted_content": "opaque-tool-call-state",
                },
                {"type": "function_call_output", "call_id": "call_program_owned", "output": "done"},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]},
            ],
            id="encrypted-program-tool-call",
        ),
        pytest.param(
            [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "run it"}]},
                {
                    "type": "function_call",
                    "call_id": "call_program_unpaired",
                    "name": "lookup",
                    "arguments": "{}",
                    "caller": {"type": "program", "caller_id": "prog_owner_bound"},
                    "encrypted_content": "opaque-tool-call-state",
                },
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]},
            ],
            id="encrypted-unpaired-program-tool-call",
        ),
        pytest.param(
            [
                {
                    "type": "reasoning",
                    "id": "rs_with_caller",
                    "summary": [],
                    "caller": {"type": "program", "caller_id": "prog_owner_bound"},
                    "encrypted_content": "opaque-reasoning-state",
                },
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]},
            ],
            id="reasoning-with-foreign-field",
        ),
    ],
)
async def test_stream_encrypted_non_ciphertext_state_429_remains_on_first_dispatch_account(
    async_client, monkeypatch, input_items
):
    """Ciphertext only releases ownership on a supported item shape; other fields stay validated."""
    await _import_account(async_client, "acc_stream_enc_tool_429_a", "streamenctool429a@example.com")
    await _import_account(async_client, "acc_stream_enc_tool_429_b", "streamenctool429b@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        raise ProxyResponseError(
            429,
            openai_error("usage_limit_reached", "usage limit reached"),
            failure_phase="status",
        )
        yield  # pragma: no cover - retain the async-generator transport contract

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    response = await async_client.post(
        "/backend-api/codex/responses",
        json={
            "model": "gpt-5.1",
            "input": input_items,
            "prompt_cache_key": "cache_encrypted_tool_429",
            "stream": True,
        },
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "usage_limit_reached"
    assert len(seen_account_ids) == 1


@pytest.mark.asyncio
async def test_stream_encrypted_reasoning_429_cross_account_failover_is_unchanged(async_client, monkeypatch):
    """A rejected pending owner may fail over without rewriting ciphertext."""
    await _import_account(async_client, "acc_stream_reasoning_429_a", "reasoning429a@example.com")
    await _import_account(async_client, "acc_stream_reasoning_429_b", "reasoning429b@example.com")

    encrypted_content = "opaque-authenticated-encrypted-reasoning"
    seen_account_ids: list[str | None] = []
    seen_ciphertexts: list[str] = []
    rejected_account_id: str | None = None

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        nonlocal rejected_account_id
        seen_account_ids.append(account_id)
        assert isinstance(payload.input, list)
        reasoning_item = payload.input[0]
        assert isinstance(reasoning_item, dict)
        assert reasoning_item["type"] == "reasoning"
        seen_ciphertexts.append(reasoning_item["encrypted_content"])
        if rejected_account_id is None:
            rejected_account_id = account_id
            raise ProxyResponseError(
                429,
                openai_error("usage_limit_reached", "usage limit reached"),
                failure_phase="status",
            )
        assert account_id != rejected_account_id
        yield _success_sse_event("resp_stream_reasoning_429_ok")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.1",
        "input": [
            {
                "type": "reasoning",
                "id": "rs_cross_account",
                "summary": [],
                "encrypted_content": encrypted_content,
            },
            {"role": "user", "content": "continue"},
        ],
        "prompt_cache_key": "cache_encrypted_reasoning_429",
        "stream": True,
    }
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    assert len([e for e in events if e.get("type") == "response.completed"]) == 1
    assert not [e for e in events if e.get("type") == "response.failed"]
    assert len(seen_account_ids) == 2
    assert seen_account_ids[0] != seen_account_ids[1]
    assert seen_ciphertexts == [encrypted_content, encrypted_content]


@pytest.mark.parametrize("encrypted_item_type", ["reasoning", "compaction"])
@pytest.mark.parametrize("rejection_shape", ["status_observed", "response_failed_observed", "status_literal"])
@pytest.mark.asyncio
async def test_stream_cross_account_encrypted_reasoning_rejection_logs_failover_provenance(
    async_client, monkeypatch, caplog, encrypted_item_type: str, rejection_shape: str
):
    """An upstream portability reversal is attributable to the quota failover that exposed it."""
    account_ids = {
        "acc_reasoning_diag_a": await _import_account(
            async_client, "acc_reasoning_diag_a", "reasoningdiaga@example.com"
        ),
        "acc_reasoning_diag_b": await _import_account(
            async_client, "acc_reasoning_diag_b", "reasoningdiagb@example.com"
        ),
    }

    rejected_account_id: str | None = None
    target_account_id: str | None = None
    rejection_code = "invalid_encrypted_content" if rejection_shape == "status_literal" else "invalid_request_error"

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        nonlocal rejected_account_id, target_account_id
        if rejected_account_id is None:
            rejected_account_id = account_id
            raise ProxyResponseError(
                429,
                openai_error("usage_limit_reached", "usage limit reached"),
                failure_phase="status",
            )
        assert account_id != rejected_account_id
        target_account_id = account_id
        rejection_message = "Reasoning content could not be decrypted"
        if rejection_shape.startswith("status_"):
            raise ProxyResponseError(
                400,
                openai_error(rejection_code, rejection_message),
                failure_phase="status",
            )
        yield _sse_event(
            {
                "type": "response.failed",
                "response": {
                    "id": "resp_reasoning_rejected",
                    "error": {
                        "code": rejection_code,
                        "message": rejection_message,
                    },
                },
            }
        )

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)
    caplog.set_level(logging.WARNING, logger="app.modules.proxy.service")

    encrypted_item = {
        "type": encrypted_item_type,
        "encrypted_content": "opaque-authenticated-encrypted-reasoning",
    }
    if encrypted_item_type == "reasoning":
        encrypted_item.update({"id": "rs_cross_account_rejected", "summary": []})

    response = await async_client.post(
        "/backend-api/codex/responses",
        json={
            "model": "gpt-5.1",
            "input": [
                encrypted_item,
                {"role": "user", "content": "continue"},
            ],
            "stream": True,
        },
    )

    assert response.status_code == (400 if rejection_shape.startswith("status_") else 200)
    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("cross_account_encrypted_reasoning_rejected ")
    ]
    assert len(records) == 1
    diagnostic = records[0].getMessage()
    assert f"source_account_id={rejected_account_id}" in diagnostic
    assert f"target_account_id={target_account_id}" in diagnostic
    assert "failover_trigger=previsible_rate_limit_or_quota" in diagnostic
    assert f"upstream_code={rejection_code}" in diagnostic
    assert "opaque-authenticated-encrypted-reasoning" not in diagnostic
    assert target_account_id is not None
    async with SessionLocal() as session:
        target_account = await session.get(Account, account_ids[target_account_id])
        assert target_account is not None
        assert target_account.status == AccountStatus.ACTIVE
    from app.dependencies import get_proxy_service_for_app

    service = get_proxy_service_for_app(async_client._transport.app)
    target_runtime = service._load_balancer._runtime.get(account_ids[target_account_id])
    assert target_runtime is None or target_runtime.error_count == 0


@pytest.mark.asyncio
async def test_stream_invalid_encrypted_reasoning_without_failover_has_no_cross_account_diagnostic(
    async_client, monkeypatch, caplog
):
    """An ordinarily malformed blob must not be attributed to cross-account failover."""
    await _import_account(async_client, "acc_reasoning_diag_direct", "reasoningdiagdirect@example.com")

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        raise ProxyResponseError(
            400,
            openai_error("invalid_encrypted_content", "Encrypted reasoning content is invalid"),
            failure_phase="status",
        )
        yield  # pragma: no cover - retain the async-generator transport contract

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)
    caplog.set_level(logging.WARNING, logger="app.modules.proxy.service")

    response = await async_client.post(
        "/backend-api/codex/responses",
        json={
            "model": "gpt-5.1",
            "input": [
                {
                    "type": "reasoning",
                    "id": "rs_direct_rejected",
                    "summary": [],
                    "encrypted_content": "malformed-encrypted-reasoning",
                }
            ],
            "stream": True,
        },
    )

    assert response.status_code == 400
    assert "cross_account_encrypted_reasoning_rejected" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("frame_error", _USAGE_LIMIT_FRAME_ERRORS)
async def test_stream_compacted_input_first_event_usage_limit_transparent_failover(
    async_client, monkeypatch, frame_error
):
    """A first-event quota rejection must not bind retained input to the rejected account."""
    await _import_account(async_client, "acc_stream_event_limit_a", "streameventlimita@example.com")
    await _import_account(async_client, "acc_stream_event_limit_b", "streameventlimitb@example.com")

    seen_account_ids: list[str | None] = []
    rejected_account_id: str | None = None

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        nonlocal rejected_account_id
        seen_account_ids.append(account_id)
        if rejected_account_id is None:
            rejected_account_id = account_id
            yield _sse_event(
                {
                    "type": "response.failed",
                    "response": {
                        "error": frame_error,
                    },
                }
            )
            return
        assert account_id != rejected_account_id
        yield _success_sse_event("resp_stream_event_limit_ok")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.1",
        "instructions": "describe the image",
        "input": [
            {"type": "compaction", "encrypted_content": "opaque-compacted-history"},
            {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo="},
        ],
        "prompt_cache_key": "cache_compacted_image_event_limit",
        "stream": True,
    }
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    completed = [e for e in events if e.get("type") == "response.completed"]
    failed = [e for e in events if e.get("type") == "response.failed"]
    assert len(completed) == 1
    assert len(failed) == 0
    assert len(seen_account_ids) == 2
    assert seen_account_ids[0] != seen_account_ids[1]


@pytest.mark.asyncio
async def test_stream_code_less_429_retries_same_account_then_succeeds(async_client, monkeypatch):
    """Code-less 429 (upstream burst) on an owner-bound payload backs off and retries the owner.

    Contrast with the coded-429 test above: the account is never marked
    RATE_LIMITED and the request never crosses accounts (``reasoning`` input
    items bind the dispatched payload to the first account).
    """
    account_a_id = await _import_account(async_client, "acc_stream_burst_a", "streambursta@example.com")
    await _import_account(async_client, "acc_stream_burst_b", "streamburstb@example.com")

    seen_account_ids: list[str | None] = []
    slept = _record_burst_backoff_sleeps(monkeypatch)

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if len(seen_account_ids) == 1:
            raise ProxyResponseError(
                429,
                {"error": {"message": "Rate limit exceeded"}},
                failure_phase="status",
            )
        yield _success_sse_event("resp_stream_burst_ok")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.1",
        "instructions": "hi",
        "input": [{"type": "reasoning", "id": "rs_stream_burst", "encrypted_content": "owner-bound"}],
        "stream": True,
    }
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    completed = [e for e in events if e.get("type") == "response.completed"]
    failed = [e for e in events if e.get("type") == "response.failed"]
    assert len(completed) == 1
    assert len(failed) == 0
    assert seen_account_ids == ["acc_stream_burst_a", "acc_stream_burst_a"]
    assert 1.0 in slept

    async with SessionLocal() as session:
        burst_account = await session.get(Account, account_a_id)
        assert burst_account is not None
        assert burst_account.status == AccountStatus.ACTIVE


def _record_burst_backoff_sleeps(monkeypatch) -> list[float]:
    """Intercept the bounded burst backoff through the scheduler seam it actually uses.

    The wait runs through ``RealScheduler.sleep`` (``scheduler_for(proxy)``), so
    patching that seam observes exactly the backoff schedule. Patching the
    process-wide ``asyncio.sleep`` instead would also reach the lifespan
    ``run_event_loop_lag_monitor`` task, whose ``asyncio.sleep(1.0)`` loop then
    spins without yielding for the rest of the test and floods the recording.
    The fake still yields once so the loop keeps turning.
    """
    slept: list[float] = []
    real_sleep = asyncio.sleep

    def fake_sleep(self, delay: float, result: None = None):
        if delay > 0:
            slept.append(delay)
        return real_sleep(0, result)

    monkeypatch.setattr(RealScheduler, "sleep", fake_sleep)
    return slept


_NATIVE_CODEX_HEADERS = {"originator": "codex_cli_rs"}
_BURST_OWNER_BOUND_PAYLOAD = {
    "model": "gpt-5.1",
    "instructions": "hi",
    "input": [{"type": "reasoning", "id": "rs_stream_burst_native", "encrypted_content": "owner-bound"}],
    "stream": True,
}


def _install_always_burst_upstream(monkeypatch, *, retry_after_seconds=None, retry_after_header=None):
    seen_account_ids: list[str | None] = []
    slept = _record_burst_backoff_sleeps(monkeypatch)

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        raise ProxyResponseError(
            429,
            {"error": {"message": "Rate limit exceeded"}},
            failure_phase="status",
            retry_after_seconds=retry_after_seconds,
            retry_after_header=retry_after_header,
        )
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)
    return seen_account_ids, slept


@pytest.mark.asyncio
async def test_native_codex_stream_code_less_429_exhaustion_surfaces_http_429_with_retry_after(
    async_client, monkeypatch, caplog
):
    """Codex CLI (native headers, propagate_http_errors, no SDK contract): the bounded same-account
    backoff must hold the HTTP headers -- no keepalive frame commits a 200 -- so the exhausted
    burst is still a real HTTP 429 carrying ``Retry-After: 5``."""
    await _import_account(async_client, "acc_stream_burst_native_a", "streamburstnativea@example.com")
    await _import_account(async_client, "acc_stream_burst_native_b", "streamburstnativeb@example.com")
    seen_account_ids, slept = _install_always_burst_upstream(monkeypatch)
    caplog.set_level("INFO", logger="app.modules.proxy.service")

    async with async_client.stream(
        "POST", "/backend-api/codex/responses", json=_BURST_OWNER_BOUND_PAYLOAD, headers=_NATIVE_CODEX_HEADERS
    ) as resp:
        body = json.loads(await resp.aread())
        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == "5"

    assert body == {"error": {"message": "Rate limit exceeded"}}
    # Never crossed accounts; three backoffs then the original rejection.
    assert len(seen_account_ids) == 4
    assert len(set(seen_account_ids)) == 1
    assert slept == [1.0, 2.0, 4.0]
    assert caplog.text.count("action=retry_same_account") == 3
    assert "failure_class=retryable_transient action=surface" in caplog.text
    assert "action=failover_next" not in caplog.text


@pytest.mark.asyncio
async def test_native_codex_stream_code_less_429_exhaustion_preserves_upstream_retry_after(async_client, monkeypatch):
    await _import_account(async_client, "acc_stream_burst_native_ra", "streamburstnativera@example.com")
    _seen, slept = _install_always_burst_upstream(monkeypatch, retry_after_seconds=3, retry_after_header="3")

    async with async_client.stream(
        "POST", "/backend-api/codex/responses", json=_BURST_OWNER_BOUND_PAYLOAD, headers=_NATIVE_CODEX_HEADERS
    ) as resp:
        await resp.aread()
        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == "3"

    # Retry-After floors the schedule: 3, 3, then the exponential 4.
    assert slept == [3.0, 3.0, 4.0]


@pytest.mark.asyncio
async def test_native_codex_stream_code_less_429_retry_success_emits_no_capacity_keepalive(async_client, monkeypatch):
    """The held-header wait must not leak a ``waiting_for_account_capacity`` keepalive into the stream."""
    await _import_account(async_client, "acc_stream_burst_native_ok", "streamburstnativeok@example.com")
    seen_account_ids: list[str | None] = []
    slept = _record_burst_backoff_sleeps(monkeypatch)

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if len(seen_account_ids) == 1:
            raise ProxyResponseError(429, {"error": {"message": "Rate limit exceeded"}}, failure_phase="status")
        yield _success_sse_event("resp_stream_burst_native_ok")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    async with async_client.stream(
        "POST", "/backend-api/codex/responses", json=_BURST_OWNER_BOUND_PAYLOAD, headers=_NATIVE_CODEX_HEADERS
    ) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    assert [e["type"] for e in events if e.get("type") == "response.completed"] == ["response.completed"]
    assert all(e.get("status") != "waiting_for_account_capacity" for e in events)
    assert seen_account_ids == ["acc_stream_burst_native_ok", "acc_stream_burst_native_ok"]
    assert 1.0 in slept


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_delivery", ["sse", "status"])
@pytest.mark.parametrize("failure_code", ["usage_limit_reached", "quota_exceeded"])
async def test_stream_first_event_429_with_owner_state_transparently_fails_over(
    async_client, monkeypatch, failure_delivery, failure_code
):
    """A rejected request must not pin its replay to the exhausted account."""
    await _import_account(async_client, "acc_sticky_429_a", "sticky429a@example.com")
    await _import_account(async_client, "acc_sticky_429_b", "sticky429b@example.com")

    seen_account_ids: list[str | None] = []
    seen_inputs: list[object] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        seen_inputs.append(payload.input)
        assert payload.model == "gpt-5.6-sol"
        assert payload.reasoning is not None
        assert payload.reasoning.effort == "xhigh"
        if account_id == "acc_sticky_429_a":
            if failure_delivery == "status":
                raise ProxyResponseError(
                    429,
                    openai_error(failure_code, "usage limit reached"),
                    failure_phase="status",
                )
            yield _sse_event(
                {
                    "type": "response.failed",
                    "response": {"error": {"code": failure_code, "message": "usage limit reached"}},
                }
            )
            return
        yield _success_sse_event("resp_sticky_429_ok")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.6-sol",
        "reasoning": {"effort": "xhigh"},
        "prompt_cache_key": "sticky-usage-limit-failover",
        "instructions": "hi",
        "input": [
            {
                "type": "message",
                "id": "msg_first_user",
                "role": "user",
                "content": [{"type": "input_text", "text": "first question"}],
            },
            {
                "type": "message",
                "id": "msg_previous_answer",
                "role": "assistant",
                "status": "completed",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "previous answer"}],
            },
            {
                "type": "reasoning",
                "id": "rs_owner",
                "encrypted_content": "owner-bound",
                "summary": [],
            },
            {
                "type": "message",
                "id": "msg_owner",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        ],
        "stream": True,
    }
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    completed = [event for event in events if event.get("type") == "response.completed"]
    failed = [event for event in events if event.get("type") == "response.failed"]
    assert len(completed) == 1
    assert len(failed) == 0
    assert seen_account_ids[:2] == ["acc_sticky_429_a", "acc_sticky_429_b"]
    first_input = seen_inputs[0]
    second_input = seen_inputs[1]
    assert isinstance(first_input, list)
    assert isinstance(second_input, list)
    assert [item.get("type") for item in first_input if isinstance(item, dict)] == [
        "message",
        "message",
        "reasoning",
        "message",
    ]
    assert [item.get("type") for item in second_input if isinstance(item, dict)] == [
        "message",
        "message",
        "message",
    ]
    assert all(isinstance(item, dict) and "id" not in item for item in second_input)


@pytest.mark.asyncio
async def test_stream_previsible_429_does_not_replay_unanchored_delta_owner_state(async_client, monkeypatch):
    """Owner bookkeeping alone is not evidence of a complete portable transcript."""
    await _import_account(async_client, "acc_delta_429_a", "delta429a@example.com")
    await _import_account(async_client, "acc_delta_429_b", "delta429b@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if account_id == "acc_delta_429_a":
            raise ProxyResponseError(
                429,
                openai_error("usage_limit_reached", "usage limit reached"),
                failure_phase="status",
            )
        yield _success_sse_event("resp_delta_should_not_replay")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.6-sol",
        "reasoning": {"effort": "xhigh"},
        "prompt_cache_key": "sticky-delta-usage-limit",
        "instructions": "hi",
        # Item-id bookkeeping without ciphertext: a ciphertext-only body is
        # released before ownership by the pre-visible quota exemption, so
        # it would not exercise the owner-bound projection.
        "input": [
            {
                "type": "reasoning",
                "id": "rs_owner",
                "summary": [],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "continue"}],
            },
        ],
        "stream": True,
    }
    response = await async_client.post("/backend-api/codex/responses", json=payload)

    assert response.status_code == 429
    assert response.json().get("error", {}).get("code") == "usage_limit_reached"
    assert seen_account_ids == ["acc_delta_429_a"]


_QUOTA_DEVELOPER = {
    "type": "message",
    "id": "msg_developer",
    "role": "developer",
    "content": [{"type": "input_text", "text": "<permissions instructions>sandbox</permissions instructions>"}],
}
_QUOTA_FIRST_USER = {
    "type": "message",
    "id": "msg_first_user",
    "role": "user",
    "content": [{"type": "input_text", "text": "first question"}],
}
_QUOTA_PREVIOUS_ANSWER = {
    "type": "message",
    "id": "msg_previous_answer",
    "role": "assistant",
    "status": "completed",
    "phase": "final_answer",
    "content": [{"type": "output_text", "text": "previous answer"}],
}
_QUOTA_OWNER_REASONING = {"type": "reasoning", "id": "rs_owner", "summary": []}
_QUOTA_NEXT_USER = {
    "type": "message",
    "id": "msg_next_user",
    "role": "user",
    "content": [{"type": "input_text", "text": "continue"}],
}


def _without_id(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != "id"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("input_items", "expected_replay_input"),
    [
        pytest.param(
            [_QUOTA_DEVELOPER, _QUOTA_FIRST_USER, _QUOTA_PREVIOUS_ANSWER, _QUOTA_OWNER_REASONING, _QUOTA_NEXT_USER],
            [
                _without_id(_QUOTA_DEVELOPER),
                _without_id(_QUOTA_FIRST_USER),
                _without_id(_QUOTA_PREVIOUS_ANSWER),
                _without_id(_QUOTA_NEXT_USER),
            ],
            id="developer-prefixed-full-resend-moves",
        ),
        pytest.param(
            [_QUOTA_DEVELOPER, _QUOTA_PREVIOUS_ANSWER, _QUOTA_OWNER_REASONING, _QUOTA_NEXT_USER],
            None,
            id="developer-only-prefix-stays-owner-bound",
        ),
    ],
)
async def test_stream_previsible_429_judges_unanchored_replay_without_inline_developer_messages(
    async_client, monkeypatch, input_items, expected_replay_input
):
    """Developer messages stay in ``input`` but are no conversation history.

    The replay decision sees the history the old hoist into ``instructions``
    left: a developer prefix does not stop a full resend from moving, and a
    developer message alone ahead of the prior answer is no user turn. The
    moved body keeps its developer messages, without their client item ids.
    """
    await _import_account(async_client, "acc_dev_429_a", "dev429a@example.com")
    await _import_account(async_client, "acc_dev_429_b", "dev429b@example.com")

    seen_account_ids: list[str | None] = []
    seen_inputs: list[object] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        seen_inputs.append(payload.input)
        if account_id == "acc_dev_429_a":
            raise ProxyResponseError(
                429,
                openai_error("usage_limit_reached", "usage limit reached"),
                failure_phase="status",
            )
        yield _success_sse_event("resp_dev_429_ok")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.6-sol",
        "prompt_cache_key": "sticky-developer-usage-limit",
        "instructions": "hi",
        "input": input_items,
        "stream": True,
    }
    response = await async_client.post("/backend-api/codex/responses", json=payload)

    if expected_replay_input is None:
        assert response.status_code == 429
        assert response.json().get("error", {}).get("code") == "usage_limit_reached"
        assert seen_account_ids == ["acc_dev_429_a"]
        return
    assert response.status_code == 200
    assert seen_account_ids == ["acc_dev_429_a", "acc_dev_429_b"]
    # The owner got the body as sent; the replacement gets it projected.
    assert _QUOTA_OWNER_REASONING in cast(list[object], seen_inputs[0])
    assert seen_inputs[1] == expected_replay_input


@pytest.mark.asyncio
async def test_stream_post_refresh_quota_rejection_projects_soft_owner_replay(async_client, monkeypatch):
    """A refreshable 401 leaves a soft owner; the post-refresh quota rejection must still move."""
    await _import_account(async_client, "acc_post_refresh_429_a", "postrefresh429a@example.com")
    await _import_account(async_client, "acc_post_refresh_429_b", "postrefresh429b@example.com")

    seen_account_ids: list[str | None] = []
    seen_inputs: list[object] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        seen_inputs.append(payload.input)
        attempts_on_first = seen_account_ids.count(seen_account_ids[0])
        if account_id == seen_account_ids[0] and attempts_on_first == 1:
            raise ProxyResponseError(401, openai_error("invalid_api_key", "token expired"), failure_phase="status")
        if account_id == seen_account_ids[0]:
            raise ProxyResponseError(
                429,
                openai_error("usage_limit_reached", "usage limit reached"),
                failure_phase="status",
            )
        yield _success_sse_event("resp_post_refresh_429_ok")

    async def fake_ensure_fresh(self, account, **kwargs):
        del self, kwargs
        return account

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)
    monkeypatch.setattr(proxy_module.ProxyService, "_ensure_fresh_with_budget", fake_ensure_fresh)

    payload = {
        "model": "gpt-5.1",
        "prompt_cache_key": "post-refresh-usage-limit",
        "instructions": "hi",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first question"}]},
            {
                "type": "message",
                "id": "msg_previous_answer",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "previous answer"}],
            },
            {"type": "reasoning", "id": "rs_soft_owner", "summary": []},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]},
        ],
        "stream": True,
    }
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    assert [event["type"] for event in events if event.get("type") in ("response.completed", "response.failed")] == [
        "response.completed"
    ]
    assert len(seen_account_ids) == 3
    assert seen_account_ids[1] == seen_account_ids[0]
    assert seen_account_ids[2] != seen_account_ids[0]
    replay_input = seen_inputs[2]
    assert isinstance(replay_input, list)
    assert all(isinstance(item, dict) and item.get("type") != "reasoning" and "id" not in item for item in replay_input)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_delivery", ["sse", "status"])
async def test_stream_post_refresh_quota_rejection_keeps_hard_owner(async_client, monkeypatch, failure_delivery):
    """A file-pinned body stays on its account after the post-refresh quota rejection."""
    await _import_account(async_client, "acc_post_refresh_hard_a", "postrefreshharda@example.com")
    await _import_account(async_client, "acc_post_refresh_hard_b", "postrefreshhardb@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if len(seen_account_ids) == 1:
            raise ProxyResponseError(401, openai_error("invalid_api_key", "token expired"), failure_phase="status")
        if failure_delivery == "status":
            raise ProxyResponseError(
                429,
                openai_error("usage_limit_reached", "usage limit reached"),
                failure_phase="status",
            )
        yield _sse_event(
            {
                "type": "response.failed",
                "response": {"error": {"code": "usage_limit_reached", "message": "usage limit reached"}},
            }
        )

    async def fake_ensure_fresh(self, account, **kwargs):
        del self, kwargs
        return account

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)
    monkeypatch.setattr(proxy_module.ProxyService, "_ensure_fresh_with_budget", fake_ensure_fresh)

    async with async_client.stream(
        "POST",
        "/backend-api/codex/responses",
        json={
            "model": "gpt-5.1",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first"}]},
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "answer"}],
                },
                {"type": "input_file", "file_id": "file_hard_owner"},
            ],
            "prompt_cache_key": "post-refresh-hard-owner",
            "stream": True,
        },
    ) as resp:
        lines = [line async for line in resp.aiter_lines() if line]

    assert len(set(seen_account_ids)) == 1
    del lines


@pytest.mark.asyncio
async def test_stream_previsible_429_replays_scheduled_heartbeat_on_another_account(async_client, monkeypatch):
    """Codex host heartbeats are fresh input, not an orphaned upstream tool result."""
    await _import_account(async_client, "acc_heartbeat_429_a", "heartbeat429a@example.com")
    await _import_account(async_client, "acc_heartbeat_429_b", "heartbeat429b@example.com")

    seen_account_ids: list[str | None] = []
    seen_inputs: list[object] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        seen_inputs.append(payload.input)
        if account_id == "acc_heartbeat_429_a":
            raise ProxyResponseError(
                429,
                openai_error("usage_limit_reached", "usage limit reached"),
                failure_phase="status",
            )
        yield _success_sse_event("resp_heartbeat_429_ok")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    def heartbeat_item(item_id: str, turn_id: str) -> dict:
        return {
            "type": "function_call_output",
            "id": item_id,
            "name": "automation_update",
            "namespace": "codex_app",
            "output": (
                "<heartbeat><automation_id>follow-pr</automation_id>"
                "<current_time_iso>2026-09-04T13:58:17Z</current_time_iso></heartbeat>"
            ),
            "internal_chat_message_metadata_passthrough": {
                "turn_id": turn_id,
                "create_time": 1_788_526_697,
            },
        }

    payload = {
        "model": "gpt-5.6-sol",
        "reasoning": {"effort": "xhigh"},
        "prompt_cache_key": "scheduled-heartbeat-failover",
        "instructions": "monitor the pull request",
        "input": [
            {"type": "message", "id": "msg_user", "role": "user", "content": "monitor this"},
            {
                "type": "message",
                "id": "msg_first_answer",
                "role": "assistant",
                "status": "completed",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "No actionable change."}],
            },
            heartbeat_item("host_heartbeat_old", "turn_old"),
            {
                "type": "message",
                "id": "msg_latest_answer",
                "role": "assistant",
                "status": "completed",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Still waiting."}],
            },
            {
                "type": "reasoning",
                "id": "rs_owner",
                "encrypted_content": "owner-bound",
                "summary": [],
            },
            heartbeat_item("host_heartbeat_current", "turn_current"),
        ],
        "stream": True,
    }

    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    assert len([event for event in events if event.get("type") == "response.completed"]) == 1
    assert not [event for event in events if event.get("type") == "response.failed"]
    assert seen_account_ids[:2] == ["acc_heartbeat_429_a", "acc_heartbeat_429_b"]
    replay_input = seen_inputs[1]
    assert isinstance(replay_input, list)
    assert all(isinstance(item, dict) and "id" not in item for item in replay_input)
    assert all(item.get("type") != "reasoning" for item in replay_input if isinstance(item, dict))
    replay_message_roles = [
        item.get("role") for item in replay_input if isinstance(item, dict) and item.get("type") in (None, "message")
    ]
    assert replay_message_roles == ["user", "assistant", "assistant"]
    replay_heartbeats = [
        item for item in replay_input if isinstance(item, dict) and item.get("namespace") == "codex_app"
    ]
    assert len(replay_heartbeats) == 2


@pytest.mark.asyncio
async def test_stream_previsible_429_does_not_replay_malformed_scheduled_heartbeat(
    async_client,
    monkeypatch,
):
    await _import_account(async_client, "acc_bad_heartbeat_429_a", "bad-heartbeat429a@example.com")
    await _import_account(async_client, "acc_bad_heartbeat_429_b", "bad-heartbeat429b@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if account_id == "acc_bad_heartbeat_429_a":
            raise ProxyResponseError(
                429,
                openai_error("usage_limit_reached", "usage limit reached"),
                failure_phase="status",
            )
        yield _success_sse_event("resp_bad_heartbeat_should_not_replay")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.6-sol",
        "reasoning": {"effort": "xhigh"},
        "prompt_cache_key": "malformed-scheduled-heartbeat",
        "instructions": "monitor the pull request",
        "input": [
            {"type": "message", "id": "msg_user", "role": "user", "content": "monitor this"},
            {
                "type": "message",
                "id": "msg_answer",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Still waiting."}],
            },
            {
                "type": "function_call_output",
                "id": "host_heartbeat_malformed",
                "name": "automation_update",
                "namespace": "codex_app",
                "output": "<heartbeat></heartbeat>trailing-data",
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": "turn_current",
                    "create_time": 1_788_526_697,
                },
            },
        ],
        "stream": True,
    }
    response = await async_client.post("/backend-api/codex/responses", json=payload)

    assert response.status_code == 429
    assert response.json().get("error", {}).get("code") == "usage_limit_reached"
    assert seen_account_ids == ["acc_bad_heartbeat_429_a"]


@pytest.mark.asyncio
async def test_stream_http_502_unknown_code_fails_over_to_second_account(async_client, monkeypatch):
    await _import_account(async_client, "acc_h502_a", "h502_a@example.com")
    await _import_account(async_client, "acc_h502_b", "h502_b@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if account_id == "acc_h502_a":
            raise ProxyResponseError(
                502,
                openai_error("bad_gateway", "Bad gateway"),
                failure_phase="status",
            )
        yield _success_sse_event()

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    completed = [e for e in events if e.get("type") == "response.completed"]
    assert len(completed) == 1
    assert seen_account_ids[:2] == ["acc_h502_a", "acc_h502_b"]


# ===========================================================================
# Streaming — Model-entitlement rejection is health neutral through the route
# ===========================================================================


_MODEL_ENTITLEMENT_REJECTION_MESSAGE = "The 'gpt-5.1' model is not supported when using Codex with a ChatGPT account."
_SAFETY_POLICY_REJECTION_MESSAGE = (
    "This request was blocked by our safety systems. Reason: Potentially unintended activity."
)


@pytest.mark.asyncio
async def test_stream_model_entitlement_rejection_keeps_account_health_after_failover(async_client, monkeypatch):
    """Routed regression: the entitlement 400 must not move any account's health.

    The unit tests call ``_handle_stream_error`` directly; this drives the whole
    HTTP route through selection, dispatch and failover with the exact upstream
    rejection observed in the incident — HTTP 400, message only, neither
    ``code`` nor ``type`` — and asserts the accounts that served the attempts
    keep ``error_count`` 0 and no error backoff. It fails if any transport path
    loses the message before the neutrality check or writes account health
    somewhere ``_handle_stream_error`` is not consulted.
    """
    account_id_1 = await _import_account(async_client, "acc_entitle_a", "entitle_a@example.com")
    account_id_2 = await _import_account(async_client, "acc_entitle_b", "entitle_b@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        raise ProxyResponseError(
            400,
            {"error": {"message": _MODEL_ENTITLEMENT_REJECTION_MESSAGE}},
            failure_phase="status",
        )
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        lines = [line async for line in resp.aiter_lines() if line]

    # Failover is untouched: another account may hold a different entitlement,
    # so the rejection must fan out before it surfaces to the client.
    assert set(seen_account_ids) == {"acc_entitle_a", "acc_entitle_b"}
    events = _extract_events(lines)
    assert not any(e.get("type") == "response.completed" for e in events)

    from app.dependencies import get_proxy_service_for_app

    service = get_proxy_service_for_app(async_client._transport.app)
    for imported_account_id in (account_id_1, account_id_2):
        runtime = service._load_balancer._runtime.get(imported_account_id)
        assert runtime is None or runtime.error_count == 0, (
            f"model-scoped rejection must not move error_count for {imported_account_id}"
        )
        assert runtime is None or runtime.last_error_at is None, (
            f"model-scoped rejection must not arm error backoff for {imported_account_id}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [None, 400, 500])
async def test_stream_safety_policy_rejection_keeps_account_health_and_original_failure(
    async_client,
    monkeypatch,
    status_code,
    caplog,
):
    """A routed policy block is request-scoped and must not poison its account."""
    caplog.set_level(logging.INFO, logger=proxy_module.logger.name)
    caplog.clear()
    imported_account_id = await _import_account(
        async_client,
        "acc_safety_policy",
        "safety-policy@example.com",
    )
    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if status_code is None:
            yield _sse_event({"type": "response.created", "response": {"id": "resp_safety_policy"}})
            yield _sse_event(
                {
                    "type": "response.failed",
                    "response": {
                        "error": {
                            "code": "misalignment_policy_violation",
                            "message": _SAFETY_POLICY_REJECTION_MESSAGE,
                        }
                    },
                }
            )
            return
        raise ProxyResponseError(
            status_code,
            openai_error(
                "misalignment_policy_violation",
                _SAFETY_POLICY_REJECTION_MESSAGE,
                error_type="invalid_request_error",
            ),
            failure_phase="status",
        )

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == (200 if status_code is None else status_code)
        lines = [line async for line in resp.aiter_lines() if line] if status_code is None else []
        response_body = None
        if status_code == 400:
            await resp.aread()
            response_body = resp.json()

    if status_code is None:
        events = _extract_events(lines)
        failed = [event for event in events if event.get("type") == "response.failed"]
        assert len(failed) == 1
        assert failed[0]["response"]["error"] == {
            "code": "misalignment_policy_violation",
            "message": _SAFETY_POLICY_REJECTION_MESSAGE,
        }
        assert len(seen_account_ids) == 1, "a non-retryable policy block must not fan out"
        assert "Skipped account error penalty for account-neutral request rejection" in caplog.text
    elif status_code == 400:
        assert response_body is not None
        assert response_body["error"]["code"] == "misalignment_policy_violation"
        assert response_body["error"]["message"] == _SAFETY_POLICY_REJECTION_MESSAGE

    from app.dependencies import get_proxy_service_for_app

    service = get_proxy_service_for_app(async_client._transport.app)
    runtime = service._load_balancer._runtime.get(imported_account_id)
    if status_code in (None, 400):
        assert runtime is None or runtime.error_count == 0
        assert runtime is None or runtime.last_error_at is None
    else:
        assert runtime is not None
        assert runtime.error_count >= 1


_STREAM_REQUIRED_WIRE_BODY = {"detail": "Stream must be set to true"}


def _upstream_400(body: dict[str, Any]) -> ProxyResponseError:
    """A ProxyResponseError built from the upstream wire body the way the client builds it."""
    from app.core.clients.proxy import _error_payload_from_response_body

    response = cast(Any, SimpleNamespace(status=400, reason="Bad Request"))
    return ProxyResponseError(
        400,
        _error_payload_from_response_body(response, data=body, text=None),
        failure_phase="status",
    )


def _assert_no_account_health_change(async_client, account_ids: list[str]) -> None:
    from app.dependencies import get_proxy_service_for_app

    service = get_proxy_service_for_app(async_client._transport.app)
    for imported_account_id in account_ids:
        runtime = service._load_balancer._runtime.get(imported_account_id)
        assert runtime is None or runtime.error_count == 0, imported_account_id
        assert runtime is None or runtime.last_error_at is None, imported_account_id


async def _enable_keyed_requests(async_client) -> dict[str, str]:
    settings_response = await async_client.put("/api/settings", json={"apiKeyAuthEnabled": True})
    assert settings_response.status_code == 200
    created = await async_client.post(
        "/api/api-keys/",
        json={
            "name": "request-shape",
            "limits": [{"limitType": "total_tokens", "limitWindow": "weekly", "maxValue": 1_000_000}],
        },
    )
    assert created.status_code == 200
    return {"authorization": f"Bearer {created.json()['key']}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("keyed", [False, True])
async def test_stream_request_shape_400_surfaces_without_failover_or_account_penalty(async_client, monkeypatch, keyed):
    """``Stream must be set to true`` rejects the request itself, so it reproduces on every account.

    It must surface from the first account, without walking the pool and
    without moving any account's health, on both the unkeyed and the keyed
    (API-key reservation) stream paths. The sticky prompt-cache owner is the
    account that answered; nothing reallocates it.
    """
    account_ids = [
        await _import_account(async_client, "acc_shape_a", "shape_a@example.com"),
        await _import_account(async_client, "acc_shape_b", "shape_b@example.com"),
        await _import_account(async_client, "acc_shape_c", "shape_c@example.com"),
    ]
    headers = await _enable_keyed_requests(async_client) if keyed else {}
    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        raise _upstream_400(_STREAM_REQUIRED_WIRE_BODY)
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.1",
        "instructions": "hi",
        "input": [],
        "stream": True,
        "prompt_cache_key": "request-shape-cache-key",
    }
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload, headers=headers) as resp:
        body = (await resp.aread()).decode()

    assert len(seen_account_ids) == 1, f"request-shape 400 must not fail over: {seen_account_ids}"
    assert "Stream must be set to true" in body
    _assert_no_account_health_change(async_client, account_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "upstream_stream"),
    [
        # The production trigger: the Codex route forwards ``stream: false``
        # upstream on purpose, and upstream rejects it with this sentence.
        ("/backend-api/codex/responses", False),
        # ``/v1/responses`` streams upstream and aggregates; here the 400 is
        # injected, proving it propagates through aggregation the same way.
        ("/v1/responses", True),
    ],
)
async def test_stream_false_request_shape_400_surfaces_without_failover_or_account_penalty(
    async_client, monkeypatch, route, upstream_stream
):
    account_ids = [
        await _import_account(async_client, f"acc_stream_false_{suffix}", f"stream_false_{suffix}@example.com")
        for suffix in "abc"
    ]
    seen_account_ids: list[str | None] = []
    upstream_stream_flags: list[object] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        upstream_stream_flags.append(payload.stream)
        raise _upstream_400(_STREAM_REQUIRED_WIRE_BODY)
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    response = await async_client.post(
        route,
        json={"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": False},
    )

    assert upstream_stream_flags == [upstream_stream]
    assert response.status_code == 400
    assert "Stream must be set to true" in response.text
    assert len(seen_account_ids) == 1, seen_account_ids
    _assert_no_account_health_change(async_client, account_ids)


@pytest.mark.asyncio
async def test_post_refresh_request_shape_400_surfaces_without_failover_or_account_penalty(async_client, monkeypatch):
    """The redispatch after a forced token refresh classifies the same rejection the same way."""
    account_ids = [
        await _import_account(async_client, "acc_shape_refresh_a", "shape_refresh_a@example.com"),
        await _import_account(async_client, "acc_shape_refresh_b", "shape_refresh_b@example.com"),
    ]
    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if len(seen_account_ids) == 1:
            raise ProxyResponseError(401, {"error": {"code": "invalid_api_key", "message": "token invalidated"}})
        raise _upstream_400(_STREAM_REQUIRED_WIRE_BODY)
        yield  # pragma: no cover - makes this an async generator

    async def fake_ensure_fresh(self, account, **kwargs):
        return account

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)
    monkeypatch.setattr(proxy_module.ProxyService, "_ensure_fresh_with_budget", fake_ensure_fresh)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        body = (await resp.aread()).decode()

    assert len(seen_account_ids) == 2, seen_account_ids
    assert seen_account_ids[0] == seen_account_ids[1], "the post-refresh rejection must not fail over"
    assert "Stream must be set to true" in body

    from app.dependencies import get_proxy_service_for_app

    service = get_proxy_service_for_app(async_client._transport.app)
    owner = service._load_balancer._runtime.get(account_ids[0])
    # The 401 before the refresh records its own error as before; the
    # request-shape rejection after it adds nothing.
    assert owner is not None and owner.error_count == 1
    _assert_no_account_health_change(async_client, account_ids[1:])


@pytest.mark.asyncio
async def test_compact_request_shape_400_surfaces_without_failover_or_account_penalty(async_client, monkeypatch):
    account_ids = [
        await _import_account(async_client, "acc_shape_compact_a", "shape_compact_a@example.com"),
        await _import_account(async_client, "acc_shape_compact_b", "shape_compact_b@example.com"),
    ]
    seen_account_ids: list[str | None] = []

    async def fake_compact(payload, headers, access_token, account_id):
        seen_account_ids.append(account_id)
        raise _upstream_400(_STREAM_REQUIRED_WIRE_BODY)

    monkeypatch.setattr(proxy_module, "core_compact_responses", fake_compact)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": []}
    response = await async_client.post("/backend-api/codex/responses/compact", json=payload)

    assert response.status_code == 400
    assert "Stream must be set to true" in response.text
    assert len(seen_account_ids) == 1, seen_account_ids
    _assert_no_account_health_change(async_client, account_ids)


@pytest.mark.asyncio
async def test_stream_code_less_unsupported_parameter_400_still_fails_over(async_client, monkeypatch):
    """A parameter rejection can depend on the account or model, so it keeps failing over.

    Accounts differ in what they accept (for example ``text.verbosity``), so a
    code-less ``Unsupported parameter`` 400 from one account must not stop a
    sibling that accepts the request from serving it.
    """
    await _import_account(async_client, "acc_param_a", "param_a@example.com")
    await _import_account(async_client, "acc_param_b", "param_b@example.com")
    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if account_id == seen_account_ids[0]:
            raise ProxyResponseError(
                400,
                {"error": {"message": "Unsupported parameter: text.verbosity"}},
                failure_phase="status",
            )
        yield _success_sse_event()

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {
        "model": "gpt-5.1",
        "instructions": "hi",
        "input": [],
        "stream": True,
        "text": {"verbosity": "high"},
    }
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    assert any(e.get("type") == "response.completed" for e in events), seen_account_ids
    assert len(seen_account_ids) == 2


@pytest.mark.asyncio
async def test_stream_model_not_found_keeps_account_health_and_valid_neighbour_working(async_client, monkeypatch):
    """A globally unknown model may exhaust failover but cannot bench its accounts."""
    account_id_1 = await _import_account(async_client, "acc_model_not_found_a", "model-not-found-a@example.com")
    account_id_2 = await _import_account(async_client, "acc_model_not_found_b", "model-not-found-b@example.com")
    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        del headers, access_token, base_url, raise_for_status
        seen_account_ids.append(account_id)
        if payload.model == "gpt-5.5":
            raise ProxyResponseError(
                404,
                openai_error(
                    "model_not_found",
                    "The model `gpt-5.5` does not exist or you do not have access to it.",
                    error_type="invalid_request_error",
                ),
                failure_phase="status",
            )
        yield _success_sse_event()

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    rejected = {"model": "gpt-5.5", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=rejected) as response:
        rejected_lines = [line async for line in response.aiter_lines() if line]

    assert set(seen_account_ids) == {"acc_model_not_found_a", "acc_model_not_found_b"}
    assert response.status_code == 404
    assert json.loads("\n".join(rejected_lines)) == {
        "error": {
            "code": "model_not_found",
            "message": "The model `gpt-5.5` does not exist or you do not have access to it.",
            "type": "invalid_request_error",
        }
    }
    assert not any(event.get("type") == "response.completed" for event in _extract_events(rejected_lines))

    from app.dependencies import get_proxy_service_for_app

    service = get_proxy_service_for_app(async_client._transport.app)
    for imported_account_id in (account_id_1, account_id_2):
        runtime = service._load_balancer._runtime.get(imported_account_id)
        assert runtime is None or runtime.error_count == 0
        assert runtime is None or runtime.last_error_at is None

    valid = {"model": "gpt-5.6", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=valid) as response:
        valid_lines = [line async for line in response.aiter_lines() if line]

    assert any(event.get("type") == "response.completed" for event in _extract_events(valid_lines))


@pytest.mark.asyncio
async def test_stream_genuine_400_failover_still_records_error_control(async_client, monkeypatch):
    """Control for the neutrality regression above: the same route and status
    with a non-entitlement message must keep penalizing, proving the runtime
    assertions would catch a health write."""
    account_id_1 = await _import_account(async_client, "acc_entitle_ctl_a", "entitle_ctl_a@example.com")

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        raise ProxyResponseError(
            400,
            {"error": {"message": "Upstream request failed"}},
            failure_phase="status",
        )
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        [line async for line in resp.aiter_lines() if line]

    from app.dependencies import get_proxy_service_for_app

    service = get_proxy_service_for_app(async_client._transport.app)
    runtime = service._load_balancer._runtime.get(account_id_1)
    assert runtime is not None and runtime.error_count >= 1, (
        "a genuine upstream 400 on the identical path must keep recording an account error"
    )


# ===========================================================================
# Streaming — Non-server_error is NOT retried via transient path
# ===========================================================================


@pytest.mark.asyncio
async def test_stream_non_server_error_not_retried_as_transient(async_client, monkeypatch):
    """A 400-class error should NOT be caught by transient retry logic."""
    await _import_account(async_client, "acc_no_trans", "notrans@example.com")

    call_count = 0

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        nonlocal call_count
        call_count += 1
        yield _sse_event(
            {
                "type": "response.failed",
                "response": {
                    "error": {
                        "code": "invalid_request_error",
                        "message": "bad request",
                    },
                },
            }
        )

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    failed = [e for e in events if e.get("type") == "response.failed"]
    assert len(failed) == 1

    # Should NOT retry — only 1 call
    assert call_count == 1


@pytest.mark.asyncio
async def test_stream_rate_limit_on_last_attempt_returns_actual_error(async_client, monkeypatch):
    """rate_limit_exceeded on the final outer attempt must yield the real error event,
    not a generic no_accounts message. Regression test for allow_retry flag separation.

    Needs 3 accounts so each outer attempt (max_attempts=3) reaches _stream_once:
    - Attempt 0: account A → rate_limit → mark A RATE_LIMITED → continue
    - Attempt 1: account B → rate_limit → mark B RATE_LIMITED → continue
    - Attempt 2 (last): account C → rate_limit → allow_retry=False →
      error event yielded to client → _TerminalStreamError → return
    """
    await _import_account(async_client, "acc_rl_a", "rla@example.com")
    await _import_account(async_client, "acc_rl_b", "rlb@example.com")
    await _import_account(async_client, "acc_rl_c", "rlc@example.com")

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        yield _sse_event(
            {
                "type": "response.failed",
                "response": {
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "slow down",
                    },
                },
            }
        )

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    # The last event the client sees should contain the actual rate_limit error
    last_event = events[-1] if events else {}
    error = last_event.get("response", {}).get("error", {})
    assert error.get("code") != "no_accounts", "Client received generic no_accounts instead of actual error"


@pytest.mark.asyncio
async def test_stream_mid_stream_error_is_surfaced_without_failover(async_client, monkeypatch):
    """Once bytes/events are emitted downstream, failover is forbidden; surface the mid-stream error."""
    await _import_account(async_client, "acc_midstream_a", "midstreama@example.com")
    await _import_account(async_client, "acc_midstream_b", "midstreamb@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if account_id == "acc_midstream_a":
            yield _sse_event({"type": "response.in_progress", "response": {"id": "resp_midstream"}})
            yield _sse_event(
                {
                    "type": "response.failed",
                    "response": {"error": {"code": "rate_limit_exceeded", "message": "mid-stream limit"}},
                }
            )
            return
        yield _success_sse_event("resp_should_not_happen")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    failed = [e for e in events if e.get("type") == "response.failed"]
    completed = [e for e in events if e.get("type") == "response.completed"]
    assert len(failed) == 1
    assert failed[0].get("response", {}).get("error", {}).get("code") == "rate_limit_exceeded"
    assert len(completed) == 0
    assert seen_account_ids == ["acc_midstream_a"]


@pytest.mark.asyncio
async def test_stream_http_500_after_text_is_surfaced_without_same_account_replay(async_client, monkeypatch):
    """A transport/status exception after visible text must not restart the answer on retry."""
    await _import_account(async_client, "acc_midtext_500", "midtext500@example.com")

    call_count = 0

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        nonlocal call_count
        call_count += 1
        yield _sse_event({"type": "response.output_text.delta", "delta": "partial answer"})
        raise ProxyResponseError(
            500,
            openai_error("server_error", "late server error"),
            failure_phase="body",
        )

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    deltas = [e for e in events if e.get("type") == "response.output_text.delta"]
    failed = [e for e in events if e.get("type") == "response.failed"]
    completed = [e for e in events if e.get("type") == "response.completed"]
    assert [e.get("delta") for e in deltas] == ["partial answer"]
    assert len(failed) == 1
    assert failed[0].get("response", {}).get("error", {}).get("code") == "server_error"
    assert completed == []
    assert call_count == 1


@pytest.mark.asyncio
async def test_stream_http_429_after_text_is_surfaced_without_account_failover(async_client, monkeypatch):
    """A quota/rate-limit exception after visible text cannot be hidden by replaying on another account."""
    await _import_account(async_client, "acc_midtext_429_a", "midtext429a@example.com")
    await _import_account(async_client, "acc_midtext_429_b", "midtext429b@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        seen_account_ids.append(account_id)
        if account_id == "acc_midtext_429_a":
            yield _sse_event({"type": "response.output_text.delta", "delta": "visible"})
            raise ProxyResponseError(
                429,
                openai_error("usage_limit_reached", "usage limit reached"),
                failure_phase="body",
            )
        yield _success_sse_event("resp_should_not_replay")

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line]

    events = _extract_events(lines)
    deltas = [e for e in events if e.get("type") == "response.output_text.delta"]
    failed = [e for e in events if e.get("type") == "response.failed"]
    completed = [e for e in events if e.get("type") == "response.completed"]
    assert [e.get("delta") for e in deltas] == ["visible"]
    assert len(failed) == 1
    assert failed[0].get("response", {}).get("error", {}).get("code") == "usage_limit_reached"
    assert completed == []
    assert seen_account_ids == ["acc_midtext_429_a"]


@pytest.mark.asyncio
async def test_v1_responses_non_streaming_500_preserves_http_status(async_client, monkeypatch):
    """Non-streaming /v1/responses uses propagate_http_errors=True.
    After exhausting transient retries, the HTTP 500 status must be preserved
    (not swallowed into a generic SSE error)."""
    await _import_account(async_client, "acc_prop_500", "prop500@example.com")

    call_count = 0

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        nonlocal call_count
        call_count += 1
        raise ProxyResponseError(
            500,
            openai_error("server_error", "An error occurred while processing your request."),
            failure_phase="status",
        )
        yield ""  # make it a generator  # pragma: no cover

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "input": "hi"}
    response = await async_client.post("/v1/responses", json=payload)
    # Must preserve the upstream 500, not 503/502
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "server_error"
    # Should have retried on same account before giving up
    assert call_count == 3


# ===========================================================================
# Compact — HTTP 500 retry
# ===========================================================================


@pytest.mark.asyncio
async def test_compact_500_succeeds_on_second_try_same_account(async_client, monkeypatch):
    """Compact 500 on 1st call → backoff retry → success on 2nd, same account."""
    await _import_account(async_client, "acc_c500_1", "c500@example.com")

    call_count = 0
    seen_account_ids: list[str | None] = []

    async def fake_compact(payload, headers, access_token, account_id):
        nonlocal call_count
        call_count += 1
        seen_account_ids.append(account_id)
        if call_count == 1:
            raise ProxyResponseError(
                500,
                openai_error("server_error", "An error occurred while processing your request."),
                failure_phase="status",
                retryable_same_contract=True,
            )
        return CompactResponsePayload.model_validate({"object": "response.compaction", "output": []})

    monkeypatch.setattr(proxy_module, "core_compact_responses", fake_compact)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": []}
    response = await async_client.post("/backend-api/codex/responses/compact", json=payload)
    assert response.status_code == 200
    assert response.json()["object"] == "response.compaction"

    assert len(seen_account_ids) == 2
    assert seen_account_ids[0] == seen_account_ids[1]


@pytest.mark.asyncio
async def test_compact_500_succeeds_on_third_try(async_client, monkeypatch):
    """Compact 500 x2, success on 3rd — all same account."""
    await _import_account(async_client, "acc_c500_3", "c500_3@example.com")

    call_count = 0
    seen_account_ids: list[str | None] = []

    async def fake_compact(payload, headers, access_token, account_id):
        nonlocal call_count
        call_count += 1
        seen_account_ids.append(account_id)
        if call_count <= 2:
            raise ProxyResponseError(
                500,
                openai_error("server_error", "server error"),
                failure_phase="status",
                retryable_same_contract=True,
            )
        return CompactResponsePayload.model_validate({"object": "response.compaction", "output": []})

    monkeypatch.setattr(proxy_module, "core_compact_responses", fake_compact)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": []}
    response = await async_client.post("/backend-api/codex/responses/compact", json=payload)
    assert response.status_code == 200

    assert len(seen_account_ids) == 3
    assert len(set(seen_account_ids)) == 1


@pytest.mark.asyncio
async def test_compact_500_exhausts_retries_then_failover(async_client, monkeypatch):
    """Compact 500 x3 on account A → failover → success on account B."""
    await _import_account(async_client, "acc_cfo_a", "cfo_a@example.com")
    await _import_account(async_client, "acc_cfo_b", "cfo_b@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_compact(payload, headers, access_token, account_id):
        seen_account_ids.append(account_id)
        if account_id == "acc_cfo_a":
            raise ProxyResponseError(
                500,
                openai_error("server_error", "server error"),
                failure_phase="status",
                retryable_same_contract=True,
            )
        return CompactResponsePayload.model_validate({"object": "response.compaction", "output": []})

    monkeypatch.setattr(proxy_module, "core_compact_responses", fake_compact)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": []}
    response = await async_client.post("/backend-api/codex/responses/compact", json=payload)
    assert response.status_code == 200

    a_calls = [aid for aid in seen_account_ids if aid == "acc_cfo_a"]
    b_calls = [aid for aid in seen_account_ids if aid == "acc_cfo_b"]
    assert len(a_calls) == 3
    assert len(b_calls) >= 1


@pytest.mark.asyncio
async def test_compact_quota_exceeded_transparent_failover(async_client, monkeypatch):
    """quota_exceeded on A should fail over to B before response write."""
    await _import_account(async_client, "acc_compact_quota_a", "compactquotaa@example.com")
    await _import_account(async_client, "acc_compact_quota_b", "compactquotab@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_compact(payload, headers, access_token, account_id):
        seen_account_ids.append(account_id)
        if account_id == "acc_compact_quota_a":
            raise ProxyResponseError(
                429,
                openai_error("quota_exceeded", "quota exceeded"),
                failure_phase="status",
            )
        return CompactResponsePayload.model_validate({"object": "response.compaction", "output": []})

    monkeypatch.setattr(proxy_module, "core_compact_responses", fake_compact)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": []}
    response = await async_client.post("/backend-api/codex/responses/compact", json=payload)
    assert response.status_code == 200
    assert response.json()["object"] == "response.compaction"
    assert seen_account_ids[:2] == ["acc_compact_quota_a", "acc_compact_quota_b"]

    async with SessionLocal() as session:
        account_id = generate_unique_account_id("acc_compact_quota_a", "compactquotaa@example.com")
        account_a = await session.get(Account, account_id)
        assert account_a is not None
        await session.refresh(account_a)
        assert account_a.status == AccountStatus.QUOTA_EXCEEDED


@pytest.mark.asyncio
async def test_compact_500_all_accounts_exhausted(async_client, monkeypatch):
    """500 on all accounts → error returned to client."""
    await _import_account(async_client, "acc_call_a", "call_a@example.com")

    async def fake_compact(payload, headers, access_token, account_id):
        raise ProxyResponseError(
            500,
            openai_error("server_error", "persistent error"),
            failure_phase="status",
            retryable_same_contract=True,
        )

    monkeypatch.setattr(proxy_module, "core_compact_responses", fake_compact)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": []}
    response = await async_client.post("/backend-api/codex/responses/compact", json=payload)
    # After exhausting all accounts, the load balancer returns 503 no_accounts
    assert response.status_code in (500, 503)


@pytest.mark.asyncio
async def test_compact_non_500_error_not_retried_as_transient(async_client, monkeypatch):
    """A 400 error should NOT be retried via the transient retry path."""
    await _import_account(async_client, "acc_c400", "c400@example.com")

    call_count = 0

    async def fake_compact(payload, headers, access_token, account_id):
        nonlocal call_count
        call_count += 1
        raise ProxyResponseError(
            400,
            openai_error("invalid_request_error", "bad request"),
            failure_phase="status",
        )

    monkeypatch.setattr(proxy_module, "core_compact_responses", fake_compact)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": []}
    response = await async_client.post("/backend-api/codex/responses/compact", json=payload)
    assert response.status_code == 400

    # Should NOT retry — only 1 call
    assert call_count == 1


@pytest.mark.asyncio
async def test_compact_502_still_uses_safe_retry_budget(async_client, monkeypatch):
    """502 errors should still use the existing safe_retry_budget (not transient path)."""
    await _import_account(async_client, "acc_c502", "c502@example.com")

    call_count = 0
    seen_account_ids: list[str | None] = []

    async def fake_compact(payload, headers, access_token, account_id):
        nonlocal call_count
        call_count += 1
        seen_account_ids.append(account_id)
        if call_count == 1:
            raise ProxyResponseError(
                502,
                openai_error("upstream_error", "bad gateway"),
                failure_phase="status",
                retryable_same_contract=True,
            )
        return CompactResponsePayload.model_validate({"object": "response.compaction", "output": []})

    monkeypatch.setattr(proxy_module, "core_compact_responses", fake_compact)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": []}
    response = await async_client.post("/backend-api/codex/responses/compact", json=payload)
    assert response.status_code == 200

    # safe_retry_budget=1 → retried once, same account
    assert call_count == 2
    assert seen_account_ids[0] == seen_account_ids[1]


@pytest.mark.asyncio
async def test_compact_sticky_503_unknown_code_excludes_failing_account_on_failover(async_client, monkeypatch):
    await _import_account(async_client, "acc_sticky_503_a", "sticky503a@example.com")
    await _import_account(async_client, "acc_sticky_503_b", "sticky503b@example.com")

    seen_account_ids: list[str | None] = []

    async def fake_compact(payload, headers, access_token, account_id):
        seen_account_ids.append(account_id)
        if account_id == "acc_sticky_503_a":
            raise ProxyResponseError(
                503,
                openai_error("bad_gateway", "Bad gateway"),
                failure_phase="status",
            )
        return CompactResponsePayload.model_validate({"object": "response.compaction", "output": []})

    monkeypatch.setattr(proxy_module, "core_compact_responses", fake_compact)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": []}
    response = await async_client.post(
        "/backend-api/codex/responses/compact",
        json=payload,
        headers={"x-codex-session-id": "sticky-session-503"},
    )
    assert response.status_code == 200
    assert response.json()["object"] == "response.compaction"
    assert seen_account_ids[:2] == ["acc_sticky_503_a", "acc_sticky_503_b"]


# ===========================================================================
# Streaming — usage_limit_reached requests an immediate usage refresh (#2123 WP-F)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.usage_refresh_request_path
async def test_stream_usage_limit_requests_immediate_refresh_so_pool_reports_exhaustion(
    async_client, app_instance, monkeypatch
):
    """A streamed usage_limit_reached marks the account rate limited *and* writes the >= 100 %
    usage row seconds later without a scheduler tick, so the next selection reports the
    structured pool exhaustion with the row's reset time instead of waiting for the next
    refresh interval.

    Runs with the production selection-cache TTL (pytest defaults it to 0): ``mark_rate_limit``
    invalidates the cache before the row exists, and a selection in between repopulates it
    without usage evidence, so the refresh must invalidate again once the row is written.
    The cross-replica invalidation poller is detached so its echo of the earlier
    ``mark_rate_limit`` bump cannot stand in for the refresh's own invalidation.
    """
    usage_updater_module._clear_usage_refresh_state()
    monkeypatch.setattr(account_cache_module, "get_cache_invalidation_poller", lambda: None)
    selection_cache = get_account_selection_cache()
    monkeypatch.setattr(selection_cache, "_ttl_seconds", 5)
    selection_cache.invalidate()
    raw_account_id = "acc_usage_limit_refresh"
    account_id = await _import_account(async_client, raw_account_id, "usage-limit-refresh@example.com")
    reset_at = int(time.time()) + 1800

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        raise ProxyResponseError(
            429,
            openai_error("usage_limit_reached", "usage limit reached"),
            failure_phase="status",
        )
        yield  # pragma: no cover - makes this an async generator

    fetched: list[str | None] = []
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()

    async def fake_fetch_usage(*, access_token, account_id, route=None, allow_direct_egress=True):
        fetched.append(account_id)
        fetch_started.set()
        await release_fetch.wait()
        return UsagePayload(
            plan_type="plus",
            rate_limit=RateLimitPayload(
                primary_window=UsageWindow(used_percent=100.0, reset_at=reset_at, limit_window_seconds=18000),
                secondary_window=UsageWindow(
                    used_percent=35.0,
                    reset_at=reset_at + 6 * 86400,
                    limit_window_seconds=604800,
                ),
            ),
        )

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)
    monkeypatch.setattr(proxy_module, "_STREAM_MAX_ACCOUNT_ATTEMPTS", 1)
    monkeypatch.setattr(proxy_api_module, "_STREAM_STARTUP_ERROR_PROBE_SECONDS", 30.0)
    monkeypatch.setattr(usage_updater_module, "fetch_usage", fake_fetch_usage)

    async def latest_primary_row():
        async with SessionLocal() as session:
            return await UsageRepository(session).latest_entry_for_account(account_id, window="primary")

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    try:
        assert await latest_primary_row() is None
        first = await async_client.post("/backend-api/codex/responses", json=payload)
        assert first.status_code == 429
        assert first.json()["error"]["code"] == "usage_limit_reached"
        await asyncio.wait_for(fetch_started.wait(), timeout=5)
        assert fetched == [raw_account_id]
        assert await latest_primary_row() is None

        # Production race: a selection between ``mark_rate_limit`` and the refresh's row write
        # repopulates the cache from a row set that carries no usage evidence yet.
        load_balancer = app_instance.state.proxy_service._load_balancer
        await load_balancer._load_selection_inputs(model=payload["model"])
        assert selection_cache._cache, "the stale selection must be cached before the row lands"
        stale_generation = selection_cache.generation

        release_fetch.set()
        # The refresh is a tracked background task: poll briefly for its row instead of a
        # scheduler tick (USAGE_REFRESH_INTERVAL_SECONDS).
        latest = None
        deadline = time.monotonic() + 5.0
        while latest is None and time.monotonic() < deadline:
            latest = await latest_primary_row()
            if latest is None:
                await asyncio.sleep(0.02)
        assert latest is not None, "a streamed usage_limit_reached must request an immediate usage refresh"
        assert latest.used_percent == 100.0
        assert latest.reset_at == reset_at

        deadline = time.monotonic() + 5.0
        while selection_cache.generation == stale_generation and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert selection_cache.generation > stale_generation, "the written row must invalidate the selection cache"
        assert selection_cache._cache == {}

        async with SessionLocal() as session:
            account = await session.get(Account, account_id)
            assert account is not None
            assert account.status in (AccountStatus.RATE_LIMITED, AccountStatus.QUOTA_EXCEEDED)

        # With the >= 100 % row written and the stale selection dropped, the pool is
        # usage-proven exhausted on the very next selection and surfaces the structured
        # failure with the row's reset time -- without waiting out the cache TTL.
        second = await async_client.post("/backend-api/codex/responses", json=payload)
        assert second.status_code == 429
        error = second.json()["error"]
        assert error["code"] == "usage_limit_reached"
        assert error["type"] == "usage_limit_reached"
        assert error["resets_at"] == reset_at
        assert fetched == [raw_account_id], "the second selection failure must not fetch upstream again"
    finally:
        release_fetch.set()
        usage_updater_module._clear_usage_refresh_state()
        selection_cache.invalidate()


# ===========================================================================
# Streaming — upstream reasoning-replay rejections are counted once per failure (#2123 CP-15)
# ===========================================================================


_REASONING_REPLAY_MESSAGE = (
    "Item with id 'rs_0f3a' of type 'reasoning' was provided without its required following item."
)


@pytest.mark.parametrize(
    ("upstream_shape", "message", "expected_increments"),
    [
        ("status_400", _REASONING_REPLAY_MESSAGE, 1),
        ("error_frame", _REASONING_REPLAY_MESSAGE, 1),
        ("error_frame_enveloped", _REASONING_REPLAY_MESSAGE, 1),
        ("response_failed_frame", _REASONING_REPLAY_MESSAGE, 1),
        ("error_frame", "Selected model is at capacity. Please try a different model.", 0),
        ("response_failed_frame", "No tool output found for function call call_abc.", 0),
    ],
)
@pytest.mark.asyncio
async def test_stream_reasoning_replay_rejection_counted_once_for_status_and_terminal_frames(
    async_client, monkeypatch, upstream_shape: str, message: str, expected_increments: int
):
    """invalid_request_error is never penalized, so terminal ``error``/``response.failed`` frames
    never reach ``_handle_stream_error``; the counter must fire at frame classification for them
    and exactly once for the HTTP-400 status path."""
    counter = MagicMock()
    monkeypatch.setattr(proxy_observability_module, "PROMETHEUS_AVAILABLE", True)
    monkeypatch.setattr(proxy_observability_module, "upstream_reasoning_replay_400_total", counter)
    await _import_account(async_client, "acc_reasoning_replay", "reasoning-replay@example.com")

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        if upstream_shape == "status_400":
            raise ProxyResponseError(
                400,
                openai_error("invalid_request_error", message),
                failure_phase="status",
            )
        if upstream_shape == "error_frame":
            yield _sse_event({"type": "error", "code": "invalid_request_error", "message": message})
        elif upstream_shape == "error_frame_enveloped":
            yield _sse_event({"type": "error", "error": {"code": "invalid_request_error", "message": message}})
        else:
            yield _sse_event(
                {
                    "type": "response.failed",
                    "response": {"error": {"code": "invalid_request_error", "message": message}},
                }
            )

    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)
    monkeypatch.setattr(proxy_module, "_STREAM_MAX_ACCOUNT_ATTEMPTS", 1)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    if upstream_shape == "status_400":
        response = await async_client.post("/backend-api/codex/responses", json=payload)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request_error"
    else:
        async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as resp:
            assert resp.status_code == 200
            lines = [line async for line in resp.aiter_lines() if line]
        events = _extract_events(lines)
        terminal = [event for event in events if event.get("type") in {"error", "response.failed"}]
        assert len(terminal) == 1

    assert counter.inc.call_count == expected_increments
