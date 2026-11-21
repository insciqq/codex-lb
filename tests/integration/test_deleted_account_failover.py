"""Accounts deleted, or imported again, between selection and dispatch.

Deleting an account removes its proxy binding by cascade, and account ids are
deterministic, so a re-import reuses the id as a new, unbound row. Every path
that dispatches with credentials it already loaded must notice either case
before sending them, instead of treating the account as unbound.
"""

from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

import app.modules.proxy.service as proxy_module
from app.core.auth import generate_unique_account_id
from app.core.crypto import TokenEncryptor
from app.core.upstream_proxy import UpstreamProxyRouteError
from app.core.utils.time import utcnow
from app.db.models import Account, AccountProxyBinding, AccountStatus, ProxyEndpoint, ProxyPool, ProxyPoolMember
from app.db.session import SessionLocal
from app.modules.accounts.repository import AccountsRepository

pytestmark = pytest.mark.integration


def _encode_jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{body}.sig"


async def _import_account(async_client, account_id: str, email: str) -> str:
    auth_json = {
        "tokens": {
            "idToken": _encode_jwt(
                {
                    "email": email,
                    "chatgpt_account_id": account_id,
                    "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
                }
            ),
            "accessToken": f"access-{account_id}",
            "refreshToken": "refresh-token",
            "accountId": account_id,
        },
    }
    files = {"auth_json": ("auth.json", json.dumps(auth_json), "application/json")}
    response = await async_client.post("/api/accounts/import", files=files)
    assert response.status_code == 200, response.text
    return generate_unique_account_id(account_id, email)


async def _delete(account_id: str, *, reimport: bool = False) -> None:
    async with SessionLocal() as writer:
        repo = AccountsRepository(writer)
        row = await writer.get(Account, account_id)
        assert row is not None
        replacement = None
        if reimport:
            token = TokenEncryptor().encrypt("replacement")
            replacement = Account(
                id=row.id,
                email=row.email,
                plan_type=row.plan_type,
                chatgpt_account_id=row.chatgpt_account_id,
                access_token_encrypted=token,
                refresh_token_encrypted=token,
                id_token_encrypted=token,
                last_refresh=utcnow(),
                status=AccountStatus.ACTIVE,
            )
        assert await repo.begin_delete(account_id)
        assert await repo.delete(account_id, only_pending=True)
        if replacement is not None:
            writer.add(replacement)
            await writer.commit()


async def _bind(account_id: str) -> None:
    async with SessionLocal() as session:
        pool = ProxyPool(id="pool", name="Pool")
        endpoint = ProxyEndpoint(id="endpoint", name="Endpoint", scheme="http", host="proxy.example.test", port=8080)
        session.add_all(
            [
                pool,
                endpoint,
                ProxyPoolMember(id="member", pool=pool, endpoint=endpoint),
                AccountProxyBinding(id="binding", account_id=account_id, pool_id="pool"),
            ]
        )
        await session.commit()


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@pytest.mark.parametrize("reimport", [False, True], ids=["deleted", "reimported"])
async def test_stream_fails_over_when_the_selected_account_is_deleted(async_client, monkeypatch, reimport):
    """The first selected account disappears after selection: the request
    moves to the other account, sends nothing with the deleted account's
    token and writes no health penalty."""
    first = await _import_account(async_client, "acc_gone_a", "gone-a@example.test")
    second = await _import_account(async_client, "acc_gone_b", "gone-b@example.test")
    await _bind(first)
    deleted: list[str] = []
    ensure_fresh = proxy_module.ProxyService._ensure_fresh_with_budget

    async def delete_first_selected(self, account, **kwargs):
        if not deleted:
            deleted.append(account.id)
            await _delete(account.id, reimport=reimport)
        return await ensure_fresh(self, account, **kwargs)

    sent: list[tuple[str | None, str]] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        sent.append((account_id, access_token))
        yield _sse({"type": "response.completed", "response": {"id": "resp_ok", "usage": {}}})

    stream_error = AsyncMock()
    monkeypatch.setattr(proxy_module.ProxyService, "_ensure_fresh_with_budget", delete_first_selected)
    monkeypatch.setattr(proxy_module.ProxyService, "_handle_stream_error", stream_error)
    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as response:
        assert response.status_code == 200
        lines = [line async for line in response.aiter_lines() if line]

    assert any('"response.completed"' in line for line in lines), lines
    raw_ids = {first: "acc_gone_a", second: "acc_gone_b"}
    kept = raw_ids[second if deleted == [first] else first]
    assert sent == [(kept, f"access-{kept}")]
    stream_error.assert_not_awaited()


async def test_compact_for_a_deleted_account_answers_retryable_selected_account_removed(async_client, monkeypatch):
    account_id = await _import_account(async_client, "acc_gone_compact", "gone-compact@example.test")
    await _bind(account_id)
    ensure_fresh = proxy_module.ProxyService._ensure_fresh_with_budget

    async def delete_first(self, account, **kwargs):
        refreshed = await ensure_fresh(self, account, **kwargs)
        await _delete(account.id)
        return refreshed

    compact = AsyncMock()
    monkeypatch.setattr(proxy_module.ProxyService, "_ensure_fresh_with_budget", delete_first)
    monkeypatch.setattr(proxy_module, "core_compact_responses", compact)

    response = await async_client.post(
        "/backend-api/codex/responses/compact",
        json={"model": "gpt-5.1", "instructions": "hi", "input": []},
    )

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "selected_account_removed"
    compact.assert_not_awaited()


async def _loaded_then_deleted(async_client, *, reimport: bool) -> Account:
    account_id = await _import_account(async_client, "acc_gone_bg", "gone-bg@example.test")
    await _bind(account_id)
    async with SessionLocal() as session:
        account = await session.get(Account, account_id)
        assert account is not None
        session.expunge(account)
    await _delete(account_id, reimport=reimport)
    return account


@pytest.mark.parametrize("reimport", [False, True], ids=["deleted", "reimported"])
@pytest.mark.parametrize("caller", ["automation", "model_discovery", "limit_warmup", "cache_isolation_probe"])
async def test_background_dispatch_route_rejects_a_deleted_account(async_client, caller, reimport):
    """Each background sender resolves the route for an account it loaded
    earlier; a deleted or re-imported row must not resolve to direct egress."""
    from app.core.openai import model_refresh_scheduler
    from app.modules.automations import service as automations
    from app.modules.cache_isolation_probe.sender import CacheProbeSender
    from app.modules.limit_warmup.service import StreamingLimitWarmupSender

    account = await _loaded_then_deleted(async_client, reimport=reimport)

    @asynccontextmanager
    async def repo_factory():
        async with SessionLocal() as session:
            yield AccountsRepository(session)

    with pytest.raises(UpstreamProxyRouteError) as exc_info:
        if caller == "automation":
            await automations._resolve_upstream_route_for_account(account, encryptor=TokenEncryptor())
        elif caller == "model_discovery":
            await model_refresh_scheduler._resolve_upstream_route_for_account(account, operation="model_discovery")
        elif caller == "limit_warmup":
            async with SessionLocal() as session:
                sender = StreamingLimitWarmupSender(AccountsRepository(session), accounts_repo_factory=repo_factory)
                await sender._resolve_upstream_route(account)
        else:
            await CacheProbeSender(repo_factory)._resolve_route(account)
    assert exc_info.value.reason == "account_not_found"


@pytest.mark.parametrize("failure", ["timeout", "database"])
async def test_stream_pre_check_failure_is_not_blamed_on_the_account(async_client, monkeypatch, failure):
    """The selected-account check right after selection is advisory: a
    database error or timeout there leaves the request on its account, with
    no failover, no health write and the usual request log."""
    from sqlalchemy import select
    from sqlalchemy.exc import OperationalError

    from app.db.models import RequestLog

    account_id = await _import_account(async_client, "acc_pre_check", "pre-check@example.test")
    resolve = proxy_module.ProxyService._resolve_upstream_route_for_account
    calls: list[str] = []

    async def failing_once(self, account, **kwargs):
        calls.append(account.id)
        if len(calls) == 1:
            if failure == "timeout":
                raise TimeoutError("command timeout")
            raise OperationalError("SELECT 1", {}, Exception("database is locked"))
        return await resolve(self, account, **kwargs)

    sent: list[str | None] = []

    async def fake_stream(payload, headers, access_token, account_id, base_url=None, raise_for_status=False):
        sent.append(account_id)
        yield _sse({"type": "response.completed", "response": {"id": "resp_pre_check", "usage": {}}})

    stream_error = AsyncMock()
    record_error = AsyncMock()
    monkeypatch.setattr(proxy_module.ProxyService, "_resolve_upstream_route_for_account", failing_once)
    monkeypatch.setattr(proxy_module.ProxyService, "_handle_stream_error", stream_error)
    monkeypatch.setattr(proxy_module.LoadBalancer, "record_error", record_error)
    monkeypatch.setattr(proxy_module, "core_stream_responses", fake_stream)

    payload = {"model": "gpt-5.1", "instructions": "hi", "input": [], "stream": True}
    async with async_client.stream("POST", "/backend-api/codex/responses", json=payload) as response:
        assert response.status_code == 200
        lines = [line async for line in response.aiter_lines() if line]

    assert any('"response.completed"' in line for line in lines), lines
    assert sent == ["acc_pre_check"]
    assert calls == [account_id, account_id]
    stream_error.assert_not_awaited()
    record_error.assert_not_awaited()
    async with SessionLocal() as session:
        logged = (await session.scalars(select(RequestLog).where(RequestLog.account_id == account_id))).all()
    assert [row.status for row in logged] == ["success"]
