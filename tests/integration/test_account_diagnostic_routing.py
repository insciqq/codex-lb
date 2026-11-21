from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.core.clients.codex import CodexTransportError
from app.core.crypto import TokenEncryptor
from app.core.utils.time import utcnow
from app.db.models import (
    Account,
    AccountProxyBinding,
    AccountStatus,
    DashboardSettings,
    ProxyEndpoint,
    ProxyPool,
    ProxyPoolMember,
    QuotaPlannerDecision,
    RequestLog,
)
from app.db.session import SessionLocal
from app.modules.accounts import api as accounts_api
from app.modules.accounts import service as accounts_service
from app.modules.accounts.auth_manager import AuthManager
from app.modules.quota_planner import warmup
from app.modules.quota_planner.logic import PlannerSettings
from app.modules.quota_planner.repository import QuotaPlannerRepository
from app.modules.usage.updater import AccountRefreshResult, UsageUpdater

pytestmark = pytest.mark.integration

# The ``transports`` fixture stubs the post-probe usage refresh; tests of that
# refresh put the real method back.
_REAL_FORCE_REFRESH_RESULT = UsageUpdater.force_refresh_result


async def _seed(*, broken: str | None = None) -> None:
    token = TokenEncryptor().encrypt("synthetic-token")
    async with SessionLocal() as session:
        for account_id in ("bound-seat", "direct-seat"):
            session.add(
                Account(
                    id=account_id,
                    email=f"{account_id}@example.test",
                    plan_type="plus",
                    chatgpt_account_id="shared-workspace",
                    chatgpt_user_id=account_id,
                    access_token_encrypted=token,
                    refresh_token_encrypted=token,
                    id_token_encrypted=token,
                    last_refresh=utcnow(),
                    status=AccountStatus.ACTIVE,
                )
            )
        pool = ProxyPool(id="bound-pool", name="Bound pool", is_active=broken != "pool")
        endpoint = ProxyEndpoint(
            id="bound-endpoint",
            name="Bound endpoint",
            scheme="http",
            host="proxy.example.test",
            port=8080,
            is_active=broken != "endpoint",
        )
        session.add_all(
            [
                pool,
                endpoint,
                ProxyPoolMember(id="member", pool=pool, endpoint=endpoint),
                AccountProxyBinding(id="binding", account_id="bound-seat", pool_id="bound-pool"),
            ]
        )
        settings = await session.get(DashboardSettings, 1)
        if settings is None:
            settings = DashboardSettings(id=1)
            session.add(settings)
        settings.upstream_proxy_routing_enabled = False
        settings.upstream_proxy_default_pool_id = None
        await session.commit()
        await QuotaPlannerRepository(session).upsert_settings(
            PlannerSettings(
                mode="auto",
                allow_synthetic_traffic=True,
                dry_run=False,
                max_warmups_per_day=5,
                max_warmup_credits_per_day=1.0,
            )
        )


@pytest.fixture
def transports(monkeypatch):
    calls = SimpleNamespace(requests=[], fail=False, status=200, release=Mock(), close=AsyncMock())

    class Response:
        @property
        def status(self):
            return calls.status

        def release(self):
            calls.release()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.release()

    class DirectSession:
        def post(self, url, **kwargs):
            calls.requests.append(("direct", None, kwargs))
            return Response()

    @asynccontextmanager
    async def lease():
        yield DirectSession()

    class RoutedClient:
        def __init__(self, session):
            pass

        async def request(self, method, url, *, route, **kwargs):
            calls.requests.append(("bound", route, kwargs))
            if calls.fail:
                raise CodexTransportError("Proxy connection failed")
            return Response()

        async def close(self):
            await calls.close()

    async def fresh(self, account, **kwargs):
        return account

    monkeypatch.setattr(accounts_service, "lease_http_session", lease)
    monkeypatch.setattr(accounts_service, "CodexClient", RoutedClient, raising=False)
    monkeypatch.setattr(accounts_service, "create_codex_session", lambda: object(), raising=False)
    monkeypatch.setattr(AuthManager, "ensure_fresh", fresh)
    monkeypatch.setattr(
        UsageUpdater,
        "force_refresh_result",
        AsyncMock(return_value=AccountRefreshResult(usage_written=False, fetch_succeeded=True)),
    )
    monkeypatch.setattr(
        accounts_api,
        "get_proxy_service_for_app",
        lambda app: SimpleNamespace(record_account_probe_result=AsyncMock()),
    )
    monkeypatch.setattr(warmup.QuotaWarmupService, "_record_warmup_effect", AsyncMock())
    return calls


@pytest.mark.parametrize("account_id", ["bound-seat", "direct-seat"])
@pytest.mark.parametrize("upstream_status", [200, 429])
async def test_force_probe_respects_local_seat_route(async_client, transports, account_id, upstream_status):
    await _seed()
    transports.status = upstream_status

    response = await async_client.post(f"/api/accounts/{account_id}/probe", json={"model": "gpt-5.4-mini"})

    assert response.status_code == 200, response.text
    assert response.json()["probeStatusCode"] == upstream_status
    assert len(transports.requests) == 1
    kind, route, kwargs = transports.requests[0]
    assert kind == ("bound" if account_id == "bound-seat" else "direct")
    assert kwargs["headers"]["chatgpt-account-id"] == "shared-workspace"
    assert kwargs["json"]["stream"] is True
    assert "max_output_tokens" not in kwargs["json"]
    transports.release.assert_called_once()
    if route is not None:
        assert route.pool_id == "bound-pool"
        assert route.endpoint_id == "bound-endpoint"
        assert kwargs["buffer_response"] is False
        transports.close.assert_awaited_once()


@pytest.mark.parametrize("broken", ["pool", "endpoint"])
async def test_force_probe_unavailable_binding_fails_before_dispatch(async_client, transports, broken):
    await _seed(broken=broken)

    response = await async_client.post("/api/accounts/bound-seat/probe")

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "upstream_proxy_unavailable"
    assert transports.requests == []


async def test_force_probe_proxy_transport_failure_never_retries_direct(async_client, transports):
    await _seed()
    transports.fail = True

    response = await async_client.post("/api/accounts/bound-seat/probe")

    assert response.status_code == 200, response.text
    assert response.json()["probeStatusCode"] == 0
    assert [kind for kind, _, _ in transports.requests] == ["bound"]
    transports.close.assert_awaited_once()
    transports.release.assert_not_called()


@pytest.mark.parametrize("account_id", ["bound-seat", "direct-seat"])
async def test_quota_warmup_routes_and_logs_local_seat(db_setup, transports, monkeypatch, account_id):
    await _seed()
    seen = []

    async def stream(payload, headers, token, upstream_id, **kwargs):
        seen.append(kwargs)
        assert upstream_id == "shared-workspace"
        assert kwargs["suppress_live_usage"] is True
        yield 'data: {"type":"response.completed","response":{"usage":{"input_tokens":3,"output_tokens":1}}}\n\n'

    monkeypatch.setattr(warmup, "stream_responses", stream)
    async with SessionLocal() as session:
        result = await warmup.QuotaWarmupService(session).warm_now(
            account_id=account_id, model="gpt-5.4-mini", force_probe=True
        )

    assert result.status == "executed", result
    assert len(seen) == 1
    route = seen[0]["route"]
    assert seen[0]["allow_direct_egress"] is (account_id == "direct-seat")
    if account_id == "bound-seat":
        assert route is not None
        assert route.pool_id == "bound-pool"
    else:
        assert route is None
    async with SessionLocal() as session:
        row = await session.scalar(select(RequestLog).where(RequestLog.request_id == result.request_id))
        assert row is not None
        assert row.status == "success"
        assert row.upstream_proxy_route_mode == ("account_bound" if route else "direct")
        assert row.upstream_proxy_pool_id == ("bound-pool" if route else None)
        assert row.upstream_proxy_endpoint_id == ("bound-endpoint" if route else None)


@pytest.mark.parametrize("failure", ["pool", "endpoint"])
async def test_quota_warmup_route_failure_is_recorded_before_dispatch(db_setup, transports, monkeypatch, failure):
    await _seed(broken=failure)
    seen = []

    async def stream(*args, **kwargs):
        seen.append(kwargs)
        yield 'data: {"type":"response.completed","response":{}}\n\n'

    monkeypatch.setattr(warmup, "stream_responses", stream)
    async with SessionLocal() as session:
        result = await warmup.QuotaWarmupService(session).warm_now(
            account_id="bound-seat", model="gpt-5.4-mini", force_probe=True
        )

    assert result.status == "failed", result
    assert not seen
    async with SessionLocal() as session:
        row = await session.scalar(select(RequestLog).where(RequestLog.request_id == result.request_id))
        decision = await session.get(QuotaPlannerDecision, result.decision_id)
        assert row is not None and row.status == "error"
        assert decision is not None and decision.status == "failed"
        expected = "pool_unavailable" if failure == "pool" else "pool_has_no_active_endpoints"
        assert row.upstream_proxy_fail_closed_reason == expected


@pytest.mark.parametrize("retryable", [False, True])
async def test_quota_warmup_routed_transport_failure_is_failed(async_client, transports, monkeypatch, retryable):
    """Drive the real stream client: a non-replayable routed transport error
    comes back as a synthetic ``response.failed`` event rather than an
    exception, and must still fail the warmup instead of recording success."""
    await _seed()
    routes = []

    async def fail(self, method_or_url, *args, route, **kwargs):
        routes.append(route)
        raise CodexTransportError(
            "Proxy connection failed",
            retryable_same_contract=retryable,
            failure_phase="connect" if retryable else "request",
        )

    monkeypatch.setattr("app.core.clients.codex.CodexClient.request_with_route_metadata", fail)
    monkeypatch.setattr("app.core.clients.codex.CodexClient.open_ws_with_route_metadata", fail)
    async with SessionLocal() as session:
        result = await warmup.QuotaWarmupService(session).warm_now(
            account_id="bound-seat", model="gpt-5.4-mini", force_probe=True
        )

    assert routes and all(route.pool_id == "bound-pool" for route in routes)
    assert result.status == "failed", result
    async with SessionLocal() as session:
        row = await session.scalar(select(RequestLog).where(RequestLog.request_id == result.request_id))
        decision = await session.get(QuotaPlannerDecision, result.decision_id)
        assert row is not None and row.status == "error"
        assert row.upstream_proxy_pool_id == "bound-pool"
        assert decision is not None and decision.status == "failed"


@pytest.mark.parametrize(
    "events",
    [
        ['data: {"type":"response.failed","response":{"error":{"code":"server_error","message":"boom"}}}\n\n'],
        ['data: {"type":"response.incomplete","response":{}}\n\n'],
        ['data: {"type":"error","error":{"code":"rate_limit_exceeded","message":"slow down"}}\n\n'],
        ['data: {"type":"response.created","response":{}}\n\n'],
        [],
    ],
    ids=["failed", "incomplete", "error", "no-terminal", "empty"],
)
async def test_quota_warmup_requires_completed_terminal(db_setup, transports, monkeypatch, events):
    await _seed()

    async def stream(*args, **kwargs):
        for event in events:
            yield event

    monkeypatch.setattr(warmup, "stream_responses", stream)
    async with SessionLocal() as session:
        result = await warmup.QuotaWarmupService(session).warm_now(
            account_id="direct-seat", model="gpt-5.4-mini", force_probe=True
        )

    assert result.status == "failed", result
    async with SessionLocal() as session:
        row = await session.scalar(select(RequestLog).where(RequestLog.request_id == result.request_id))
        decision = await session.get(QuotaPlannerDecision, result.decision_id)
        assert row is not None and row.status == "error"
        assert decision is not None and decision.status == "failed"


@pytest.mark.parametrize("broken", ["pool", "endpoint"])
async def test_force_probe_stale_token_with_unavailable_binding_returns_proxy_error(async_client, monkeypatch, broken):
    """The real AuthManager refreshes a stale token first and wraps the route
    failure in RefreshError; the probe must still report the proxy outage."""
    from datetime import timedelta

    await _seed(broken=broken)
    async with SessionLocal() as session:
        account = await session.get(Account, "bound-seat")
        assert account is not None
        account.last_refresh = utcnow() - timedelta(days=90)
        await session.commit()
    dispatched = []

    async def refresh(*args, **kwargs):
        dispatched.append("refresh")
        raise AssertionError("token refresh must not be dispatched")

    @asynccontextmanager
    async def lease():
        dispatched.append("direct")
        raise AssertionError("probe must not dispatch directly")
        yield  # pragma: no cover

    monkeypatch.setattr("app.modules.accounts.auth_manager.refresh_access_token", refresh)
    monkeypatch.setattr(accounts_service, "lease_http_session", lease)

    response = await async_client.post("/api/accounts/bound-seat/probe")

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "upstream_proxy_unavailable"
    assert dispatched == []


async def test_force_probe_reaches_the_bound_proxy_with_real_codex_client(async_client, monkeypatch):
    """End to end through CodexClient: the probe request arrives at the bound
    HTTP proxy endpoint (absolute-form request line) and nowhere else."""
    import asyncio

    from app.core.clients import codex as codex_module
    from app.core.config.settings import get_settings

    received: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        received.append(head.split(b"\r\n", 1)[0])
        writer.write(b"HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        await _seed()
        async with SessionLocal() as session:
            endpoint = await session.get(ProxyEndpoint, "bound-endpoint")
            assert endpoint is not None
            endpoint.host = "127.0.0.1"
            endpoint.port = port
            await session.commit()

        @asynccontextmanager
        async def no_direct():
            raise AssertionError("bound probe must not use the direct session")
            yield  # pragma: no cover

        monkeypatch.setattr(get_settings(), "upstream_base_url", "http://upstream.example.test/backend-api")
        monkeypatch.setattr(codex_module, "discover_native_egress_client", lambda: None)
        monkeypatch.setattr(accounts_service, "lease_http_session", no_direct)
        monkeypatch.setattr(
            UsageUpdater,
            "force_refresh_result",
            AsyncMock(return_value=AccountRefreshResult(usage_written=False, fetch_succeeded=True)),
        )
        monkeypatch.setattr(
            accounts_api,
            "get_proxy_service_for_app",
            lambda app: SimpleNamespace(record_account_probe_result=AsyncMock()),
        )

        response = await async_client.post("/api/accounts/bound-seat/probe")
    finally:
        server.close()
        await server.wait_closed()

    assert response.status_code == 200, response.text
    assert response.json()["probeStatusCode"] == 429
    assert received == [b"POST http://upstream.example.test/backend-api/codex/responses HTTP/1.1"]


async def _delete_account(account_id: str, stage: str) -> None:
    """Run the deletion worker's steps on ``account_id`` from another session.

    ``pending`` stops after the dashboard's ``begin_delete`` mark; ``finalized``
    also runs the worker's final delete, whose cascade removes the binding;
    ``reimported`` then imports the account again under the same id, as a new
    row without a binding.
    """
    from app.modules.accounts.repository import AccountsRepository

    async with SessionLocal() as writer:
        repo = AccountsRepository(writer)
        assert await repo.begin_delete(account_id)
        if stage in ("finalized", "reimported"):
            assert await repo.delete(account_id, only_pending=True)
        if stage == "reimported":
            fresh = TokenEncryptor().encrypt("replacement-token")
            writer.add(
                Account(
                    id=account_id,
                    email=f"{account_id}@example.test",
                    plan_type="plus",
                    chatgpt_account_id="shared-workspace",
                    chatgpt_user_id=account_id,
                    access_token_encrypted=fresh,
                    refresh_token_encrypted=fresh,
                    id_token_encrypted=fresh,
                    last_refresh=utcnow(),
                    status=AccountStatus.ACTIVE,
                )
            )
            await writer.commit()


def _delete_before(monkeypatch, module, account_id: str, stage: str) -> None:
    """Delete the account right before ``module``'s next route lookup runs."""
    resolve = module.resolve_upstream_route
    pending = [account_id]

    async def resolve_after_deletion(session, **kwargs):
        if pending and kwargs.get("account_id") == pending[0]:
            await _delete_account(pending.pop(), stage)
        return await resolve(session, **kwargs)

    monkeypatch.setattr(module, "resolve_upstream_route", resolve_after_deletion)


_DELETION_STAGES = pytest.mark.parametrize("stage", ["pending", "finalized", "reimported"])


@_DELETION_STAGES
async def test_force_probe_deleted_during_route_resolution_never_dispatches(
    async_client, transports, monkeypatch, stage
):
    """The account is loaded before its route is read; a deletion in between
    must not turn the bound seat into an unbound one with direct egress."""
    await _seed()
    _delete_before(monkeypatch, accounts_service, "bound-seat", stage)

    response = await async_client.post("/api/accounts/bound-seat/probe")

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "account_not_found"
    assert transports.requests == []


@_DELETION_STAGES
@pytest.mark.parametrize(
    ("method", "path", "dispatch"),
    [
        ("GET", "usage-reset-credits", "fetch_usage"),
        ("POST", "usage-reset-credits/consume", "consume_rate_limit_reset_credit"),
    ],
    ids=["read", "consume"],
)
async def test_reset_credits_deleted_during_route_resolution_answer_not_found(
    async_client, transports, monkeypatch, stage, method, path, dispatch
):
    await _seed()
    sent = AsyncMock()
    monkeypatch.setattr(accounts_service, dispatch, sent)
    _delete_before(monkeypatch, accounts_service, "bound-seat", stage)

    response = await async_client.request(method, f"/api/accounts/bound-seat/{path}")

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "account_not_found"
    sent.assert_not_awaited()


@_DELETION_STAGES
async def test_force_probe_stale_token_deleted_before_refresh_never_exchanges_direct(async_client, monkeypatch, stage):
    """The real AuthManager refreshes a stale token before the probe; the
    refresh token it holds must not leave directly once the account is gone."""
    from datetime import timedelta

    from app.modules.accounts import auth_manager

    await _seed()
    async with SessionLocal() as session:
        account = await session.get(Account, "bound-seat")
        assert account is not None
        account.last_refresh = utcnow() - timedelta(days=90)
        await session.commit()
    dispatched = []

    async def refresh(*args, **kwargs):
        dispatched.append(("refresh", kwargs.get("route")))
        raise AssertionError("token refresh must not be dispatched")

    @asynccontextmanager
    async def lease():
        dispatched.append(("direct", None))
        raise AssertionError("probe must not dispatch directly")
        yield  # pragma: no cover

    monkeypatch.setattr(auth_manager, "refresh_access_token", refresh)
    monkeypatch.setattr(accounts_service, "lease_http_session", lease)
    _delete_before(monkeypatch, auth_manager, "bound-seat", stage)

    response = await async_client.post("/api/accounts/bound-seat/probe")

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "account_not_found"
    assert dispatched == []


@_DELETION_STAGES
async def test_force_probe_usage_refresh_after_deletion_never_fetches_direct(
    async_client, transports, monkeypatch, stage
):
    """Deletion completes while the routed probe is in flight; the follow-up
    usage refresh still holds the access token and must not go direct."""
    from app.modules.usage import updater

    await _seed()
    fetch = AsyncMock()

    class DeletingClient:
        def __init__(self, session):
            pass

        async def request(self, method, url, *, route, **kwargs):
            transports.requests.append(("bound", route, kwargs))
            await _delete_account("bound-seat", stage)
            return SimpleNamespace(status=200, release=lambda: None)

        async def close(self):
            pass

    monkeypatch.setattr(accounts_service, "CodexClient", DeletingClient)
    monkeypatch.setattr(UsageUpdater, "force_refresh_result", _REAL_FORCE_REFRESH_RESULT)
    monkeypatch.setattr(updater, "fetch_usage", fetch)

    response = await async_client.post("/api/accounts/bound-seat/probe")

    assert response.status_code == 200, response.text
    assert [kind for kind, _, _ in transports.requests] == ["bound"]
    fetch.assert_not_awaited()


@_DELETION_STAGES
async def test_quota_warmup_deleted_during_route_resolution_settles_failed(db_setup, transports, monkeypatch, stage):
    await _seed()
    seen = []

    async def stream(*args, **kwargs):
        seen.append(kwargs)
        yield 'data: {"type":"response.completed","response":{}}\n\n'

    monkeypatch.setattr(warmup, "stream_responses", stream)
    _delete_before(monkeypatch, warmup, "bound-seat", stage)
    async with SessionLocal() as session:
        result = await warmup.QuotaWarmupService(session).warm_now(
            account_id="bound-seat", model="gpt-5.4-mini", force_probe=True
        )

    assert seen == []
    assert result.status == "failed", result
    async with SessionLocal() as session:
        row = await session.scalar(select(RequestLog).where(RequestLog.request_id == result.request_id))
        decision = await session.get(QuotaPlannerDecision, result.decision_id)
        assert row is not None and row.status == "error"
        assert row.account_id is None
        assert row.upstream_proxy_fail_closed_reason == "account_not_found"
        assert decision is not None and decision.status == "failed"


def _caller_token(seat: str | None) -> str:
    """An unsigned JWT whose auth claim names ``seat`` (or no seat at all)."""
    import base64
    import json

    claims = {"https://api.openai.com/auth": {"chatgpt_user_id": seat}} if seat else {"sub": "unregistered"}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"e30.{body}.sig"


async def _codex_usage(async_client, seat: str | None):
    return await async_client.get(
        "/api/codex/usage",
        headers={"Authorization": f"Bearer {_caller_token(seat)}", "chatgpt-account-id": "shared-workspace"},
    )


async def _update_seat(account_id: str, **values) -> None:
    from sqlalchemy import update

    async with SessionLocal() as session:
        await session.execute(update(Account).where(Account.id == account_id).values(**values))
        await session.commit()


def _dispatched_route(sent: AsyncMock):
    assert sent.await_args is not None
    return sent.await_args.kwargs["route"], sent.await_args.kwargs["allow_direct_egress"]


def _mock_usage_fetch(monkeypatch, payload=None) -> AsyncMock:
    from app.core.usage.models import UsagePayload

    fetch = AsyncMock(return_value=payload or UsagePayload(plan_type="plus"))
    monkeypatch.setattr("app.core.auth.dependencies.fetch_usage", fetch)
    return fetch


@pytest.mark.parametrize("stored_identity", ["column", "id_token"])
@pytest.mark.parametrize("seat", ["bound-seat", "direct-seat"])
async def test_codex_usage_identity_uses_the_callers_seat_route(async_client, monkeypatch, seat, stored_identity):
    await _seed()
    if stored_identity == "id_token":
        # Seats imported before the identity column was filled carry it only
        # in their stored ID token.
        for account_id in ("bound-seat", "direct-seat"):
            await _update_seat(
                account_id,
                chatgpt_user_id=None,
                id_token_encrypted=TokenEncryptor().encrypt(_caller_token(account_id)),
            )
    fetch = _mock_usage_fetch(monkeypatch)

    response = await _codex_usage(async_client, seat)

    assert response.status_code == 200, response.text
    fetch.assert_awaited_once()
    route, allow_direct = _dispatched_route(fetch)
    if seat == "bound-seat":
        assert route is not None and route.pool_id == "bound-pool"
        assert allow_direct is False
    else:
        assert route is None
        assert allow_direct is True


@pytest.mark.parametrize("status", [AccountStatus.PAUSED, AccountStatus.DEACTIVATED])
async def test_codex_usage_identity_rejects_an_inactive_callers_seat_before_dispatch(async_client, monkeypatch, status):
    """Pausing keeps the binding and leaves the upstream token valid; the
    paused seat's token must not borrow its active teammate's direct route."""
    await _seed()
    await _update_seat("bound-seat", status=status)
    fetch = _mock_usage_fetch(monkeypatch)

    response = await _codex_usage(async_client, "bound-seat")

    assert response.status_code == 401, response.text
    fetch.assert_not_awaited()


@pytest.mark.parametrize("bound_status", [AccountStatus.ACTIVE, AccountStatus.PAUSED])
async def test_codex_usage_identity_fails_closed_when_unmatched_seats_route_differently(
    async_client, monkeypatch, bound_status
):
    """An unidentified token could be any seat's; a bound seat counts even
    while it is paused."""
    await _seed()
    await _update_seat("bound-seat", status=bound_status)
    fetch = _mock_usage_fetch(monkeypatch)

    response = await _codex_usage(async_client, None)

    assert response.status_code == 503, response.text
    fetch.assert_not_awaited()


async def test_codex_usage_identity_uses_shared_route_when_unmatched_seats_agree(async_client, monkeypatch):
    await _seed()
    async with SessionLocal() as session:
        session.add(AccountProxyBinding(id="second-binding", account_id="direct-seat", pool_id="bound-pool"))
        await session.commit()
    fetch = _mock_usage_fetch(monkeypatch)

    response = await _codex_usage(async_client, None)

    assert response.status_code == 200, response.text
    route, allow_direct = _dispatched_route(fetch)
    assert route is not None and route.pool_id == "bound-pool"
    assert allow_direct is False


@pytest.mark.parametrize(("caller", "status"), [("bound-seat", 401), (None, 503)])
async def test_codex_usage_identity_seat_deleted_before_route_lookup_never_dispatches(
    async_client, monkeypatch, caller, status
):
    from app.core.auth import dependencies

    await _seed()
    fetch = _mock_usage_fetch(monkeypatch)
    _delete_before(monkeypatch, dependencies, "bound-seat", "finalized")

    response = await _codex_usage(async_client, caller)

    assert response.status_code == status, response.text
    fetch.assert_not_awaited()


async def test_codex_reset_credit_consume_keeps_the_identified_seat_through_workspace_reconciliation(
    async_client, monkeypatch
):
    """The workspace reply maps to a teammate's row; the caller's own seat
    must still route the consume and receive the follow-up refresh."""
    from app.core.auth import generate_unique_account_id
    from app.core.clients.usage import ConsumeRateLimitResetCreditResponse
    from app.core.usage.models import UsagePayload

    await _seed()
    teammate_id = generate_unique_account_id("shared-workspace", "direct-seat@example.test", "team-alpha")
    await _update_seat("direct-seat", id=teammate_id, workspace_id="team-alpha")
    _mock_usage_fetch(monkeypatch, UsagePayload(plan_type="plus", workspace_id="team-alpha"))
    consume = AsyncMock(return_value=ConsumeRateLimitResetCreditResponse(code="reset", windows_reset=1))
    refreshed = []

    async def force_refresh(self, account, **kwargs):
        refreshed.append(account.id)
        return False

    monkeypatch.setattr("app.modules.proxy.api.consume_rate_limit_reset_credit", consume)
    monkeypatch.setattr(UsageUpdater, "force_refresh", force_refresh)

    response = await async_client.post(
        "/api/codex/rate-limit-reset-credits/consume",
        headers={"Authorization": f"Bearer {_caller_token('bound-seat')}", "chatgpt-account-id": "shared-workspace"},
        json={"redeem_request_id": "redeem-1"},
    )

    assert response.status_code == 200, response.text
    route, allow_direct = _dispatched_route(consume)
    assert route is not None and route.pool_id == "bound-pool"
    assert allow_direct is False
    assert refreshed == ["bound-seat"]


async def test_force_probe_stale_token_deleted_before_refresh_claim_answers_not_found(async_client, monkeypatch):
    """With the production refresh-claim coordinator the claim row is written
    first; a deletion finalized before it fails that foreign key."""
    from datetime import timedelta

    from app.modules.accounts import auth_manager, refresh_claims

    await _seed()
    async with SessionLocal() as session:
        account = await session.get(Account, "bound-seat")
        assert account is not None
        account.last_refresh = utcnow() - timedelta(days=90)
        await session.commit()
    coordinator = refresh_claims.RefreshClaimCoordinator()
    acquire = coordinator.try_acquire

    async def acquire_after_deletion(account_id, **kwargs):
        await _delete_account(account_id, "finalized")
        return await acquire(account_id, **kwargs)

    refresh = AsyncMock()
    monkeypatch.setattr(coordinator, "try_acquire", acquire_after_deletion)
    monkeypatch.setattr(auth_manager, "refresh_access_token", refresh)
    monkeypatch.setattr(accounts_service, "lease_http_session", AsyncMock(side_effect=AssertionError("no probe")))
    refresh_claims.set_refresh_claim_coordinator(coordinator)

    response = await async_client.post("/api/accounts/bound-seat/probe")

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "account_not_found"
    refresh.assert_not_awaited()


@pytest.mark.parametrize("terminal", ["completed", "failed"])
async def test_quota_warmup_deleted_during_routed_stream_settles(db_setup, transports, monkeypatch, terminal):
    """Deletion finishes while the probe streams; its log can no longer
    reference the account, and the decision must still settle."""
    await _seed()

    async def stream(*args, **kwargs):
        await _delete_account("bound-seat", "finalized")
        yield f'data: {{"type":"response.{terminal}","response":{{}}}}\n\n'

    monkeypatch.setattr(warmup, "stream_responses", stream)
    async with SessionLocal() as session:
        result = await warmup.QuotaWarmupService(session).warm_now(
            account_id="bound-seat", model="gpt-5.4-mini", force_probe=True
        )

    assert result.status == ("executed" if terminal == "completed" else "failed"), result
    async with SessionLocal() as session:
        row = await session.scalar(select(RequestLog).where(RequestLog.request_id == result.request_id))
        decision = await session.get(QuotaPlannerDecision, result.decision_id)
        assert row is not None and row.account_id is None
        assert row.status == ("success" if terminal == "completed" else "error")
        assert decision is not None and decision.status == result.status
