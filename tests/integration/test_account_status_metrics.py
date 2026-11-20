"""``codex_lb_accounts_total{status}`` follows the committed account inventory.

The gauge is fed by the routing snapshot refresh (startup and every
``account_routing`` bump), the load balancer's unscoped account read
(cooldown transitions never bump ``account_routing``) and the leader's usage
refresh tick (status writes that neither of those sees). The optional Prometheus
client is not a dev dependency, so most tests swap in a recording gauge; the
scrape test runs only where ``prometheus_client`` is installed.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from app.core.metrics import prometheus as metrics
from app.core.usage import refresh_scheduler as refresh_scheduler_module
from app.db.models import Account, AccountStatus
from app.db.session import SessionLocal
from app.modules.accounts.repository import AccountsRepository
from app.modules.api_keys.repository import ApiKeysRepository
from app.modules.proxy import account_cache as account_cache_module
from app.modules.proxy.account_cache import RoutingAvailabilityCache
from app.modules.proxy.load_balancer import LoadBalancer
from app.modules.proxy.repo_bundle import ProxyRepositories
from app.modules.proxy.sticky_repository import StickySessionsRepository
from app.modules.request_logs.repository import RequestLogsRepository
from app.modules.usage.repository import AdditionalUsageRepository, UsageRepository

pytestmark = pytest.mark.integration

_ZERO = {status.value: 0.0 for status in AccountStatus}


class _Child:
    def __init__(self, gauge: _RecordingGauge, status: str) -> None:
        self._gauge = gauge
        self._status = status

    def set(self, value: float) -> None:
        self._gauge.values[self._status] = float(value)


class _RecordingGauge:
    def __init__(self) -> None:
        self.values: dict[str, float] = {}

    def labels(self, *, status: str) -> _Child:
        return _Child(self, status)


@pytest.fixture
def accounts_gauge(monkeypatch: pytest.MonkeyPatch) -> _RecordingGauge:
    gauge = _RecordingGauge()
    monkeypatch.setattr(metrics, "accounts_total", gauge)
    return gauge


def _account(account_id: str, status: AccountStatus, *, delete_requested: bool = False) -> Account:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return Account(
        id=account_id,
        chatgpt_account_id=f"workspace-{account_id}",
        email=f"{account_id}@example.com",
        plan_type="plus",
        access_token_encrypted=b"access",
        refresh_token_encrypted=b"refresh",
        id_token_encrypted=b"id",
        last_refresh=now,
        status=status,
        deactivation_reason=None,
        delete_requested_at=now if delete_requested else None,
    )


async def _insert(*accounts: Account) -> None:
    async with SessionLocal() as session:
        session.add_all(accounts)
        await session.commit()


async def _set_status(account_id: str, status: AccountStatus) -> None:
    async with SessionLocal() as session:
        await session.execute(update(Account).where(Account.id == account_id).values(status=status))
        await session.commit()


@asynccontextmanager
async def _repo_factory() -> AsyncGenerator[ProxyRepositories]:
    async with SessionLocal() as session:
        yield ProxyRepositories(
            accounts=AccountsRepository(session),
            usage=UsageRepository(session),
            request_logs=RequestLogsRepository(session),
            sticky_sessions=StickySessionsRepository(session),
            api_keys=ApiKeysRepository(session),
            additional_usage=AdditionalUsageRepository(session),
        )


async def _populate_mixed_pool() -> None:
    await _insert(
        _account("acct-active-1", AccountStatus.ACTIVE),
        _account("acct-active-2", AccountStatus.ACTIVE),
        _account("acct-limited", AccountStatus.RATE_LIMITED),
        _account("acct-quota", AccountStatus.QUOTA_EXCEEDED),
        _account("acct-paused", AccountStatus.PAUSED),
        _account("acct-reauth", AccountStatus.REAUTH_REQUIRED),
        _account("acct-deactivated", AccountStatus.DEACTIVATED),
        # Hidden from every listing; the deletion fast path also deactivates it.
        _account("acct-deleting", AccountStatus.DEACTIVATED, delete_requested=True),
    )


_MIXED_POOL_COUNTS = {
    **_ZERO,
    "active": 2.0,
    "rate_limited": 1.0,
    "quota_exceeded": 1.0,
    "paused": 1.0,
    "reauth_required": 1.0,
    "deactivated": 1.0,
}


@pytest.mark.asyncio
async def test_routing_refresh_reports_zero_for_every_status_on_empty_pool(db_setup, accounts_gauge) -> None:
    await RoutingAvailabilityCache(SessionLocal).refresh_from_db()

    assert accounts_gauge.values == _ZERO


@pytest.mark.asyncio
async def test_routing_refresh_counts_statuses_and_zeroes_emptied_ones(db_setup, accounts_gauge) -> None:
    await _populate_mixed_pool()
    cache = RoutingAvailabilityCache(SessionLocal)

    await cache.refresh_from_db()
    assert accounts_gauge.values == _MIXED_POOL_COUNTS
    # The pending-deletion row still feeds routing (unavailable), only the
    # inventory counts skip it.
    assert cache.is_unavailable("acct-deleting") is True

    await _set_status("acct-limited", AccountStatus.ACTIVE)
    await _set_status("acct-quota", AccountStatus.PAUSED)
    await cache.refresh_from_db()

    assert accounts_gauge.values == {
        **_MIXED_POOL_COUNTS,
        "active": 3.0,
        "rate_limited": 0.0,
        "quota_exceeded": 0.0,
        "paused": 2.0,
    }


@pytest.mark.asyncio
async def test_slow_routing_refresh_does_not_overwrite_newer_counts(
    db_setup, accounts_gauge, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert(_account("acct-a", AccountStatus.ACTIVE), _account("acct-b", AccountStatus.ACTIVE))
    real_close = account_cache_module.close_session
    stale_closing = asyncio.Event()
    release_stale = asyncio.Event()
    gate = [True]

    async def gated_close(session) -> None:
        if gate[0]:
            gate[0] = False
            stale_closing.set()
            await release_stale.wait()
        await real_close(session)

    monkeypatch.setattr(account_cache_module, "close_session", gated_close)
    cache = RoutingAvailabilityCache(SessionLocal)

    # The first refresh has read two active accounts and is still closing its
    # session when a status change lands and a second refresh completes.
    stale_refresh = asyncio.create_task(cache.refresh_from_db())
    await asyncio.wait_for(stale_closing.wait(), timeout=10)
    await _set_status("acct-a", AccountStatus.RATE_LIMITED)
    await cache.refresh_from_db()
    release_stale.set()
    await asyncio.wait_for(stale_refresh, timeout=10)

    assert accounts_gauge.values == {**_ZERO, "active": 1.0, "rate_limited": 1.0}


@pytest.mark.asyncio
async def test_metric_failure_does_not_abort_routing_refresh(db_setup, monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenGauge:
        def labels(self, *, status: str) -> object:
            raise RuntimeError("metric backend unavailable")

    monkeypatch.setattr(metrics, "accounts_total", _BrokenGauge())
    await _insert(_account("acct-paused", AccountStatus.PAUSED))
    cache = RoutingAvailabilityCache(SessionLocal)

    await cache.refresh_from_db()

    assert cache.seeded is True
    assert cache.is_unavailable("acct-paused") is True


@pytest.mark.asyncio
async def test_leader_usage_refresh_tick_reports_status_written_out_of_band(
    db_setup, accounts_gauge, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Leader:
        async def run_if_leader(self, fn: Callable[[], Awaitable[object]]) -> object:
            return await fn()

    monkeypatch.setattr(refresh_scheduler_module, "_get_leader_election", lambda: _Leader())
    await _insert(
        _account("acct-a", AccountStatus.ACTIVE),
        _account("acct-paused", AccountStatus.PAUSED),
        _account("acct-deleting", AccountStatus.DEACTIVATED, delete_requested=True),
    )
    await RoutingAvailabilityCache(SessionLocal).refresh_from_db()
    assert accounts_gauge.values == {**_ZERO, "active": 1.0, "paused": 1.0}

    # A reauth downgrade bumps neither account_routing nor a selection rebuild.
    await _set_status("acct-a", AccountStatus.REAUTH_REQUIRED)
    scheduler = refresh_scheduler_module.UsageRefreshScheduler(interval_seconds=60, enabled=True)
    await scheduler._refresh_once()

    # The tick counts the full listing, not just the accounts it refreshes.
    assert accounts_gauge.values == {**_ZERO, "reauth_required": 1.0, "paused": 1.0}


@pytest.mark.asyncio
async def test_selection_reports_cooldown_transition_without_routing_refresh(db_setup, accounts_gauge) -> None:
    await _insert(
        _account("acct-a", AccountStatus.ACTIVE),
        _account("acct-b", AccountStatus.ACTIVE),
        _account("acct-deleting", AccountStatus.DEACTIVATED, delete_requested=True),
    )
    await RoutingAvailabilityCache(SessionLocal).refresh_from_db()
    assert accounts_gauge.values == {**_ZERO, "active": 2.0}

    # Cooldown writes go through update_status and bump only account_selection,
    # so no routing snapshot refresh follows them.
    async with SessionLocal() as session:
        assert await AccountsRepository(session).update_status("acct-a", AccountStatus.RATE_LIMITED) is True

    selection = await LoadBalancer(_repo_factory).select_account()

    assert selection.account is not None
    assert selection.account.id == "acct-b"
    assert accounts_gauge.values == {**_ZERO, "active": 1.0, "rate_limited": 1.0}


@pytest.mark.asyncio
async def test_metrics_scrape_exposes_account_status_counts(db_setup) -> None:
    # Imported by name like the production module: the client is optional and
    # absent from the dev environment the type checker sees.
    make_asgi_app = getattr(pytest.importorskip("prometheus_client"), "make_asgi_app")
    text_string_to_metric_families = getattr(
        importlib.import_module("prometheus_client.parser"), "text_string_to_metric_families"
    )

    if metrics.MULTIPROCESS_MODE:
        pytest.skip("scrape test reads the in-process registry")
    metrics_app = make_asgi_app(registry=metrics.make_scrape_registry())

    async def scrape() -> dict[str, float]:
        async with AsyncClient(transport=ASGITransport(app=metrics_app), base_url="http://metrics") as client:
            response = await client.get("/metrics")
        assert response.status_code == 200
        return {
            sample.labels["status"]: sample.value
            for family in text_string_to_metric_families(response.text)
            if family.name == "codex_lb_accounts_total"
            for sample in family.samples
        }

    cache = RoutingAvailabilityCache(SessionLocal)
    await cache.refresh_from_db()
    assert await scrape() == _ZERO

    await _populate_mixed_pool()
    await cache.refresh_from_db()
    assert await scrape() == _MIXED_POOL_COUNTS
