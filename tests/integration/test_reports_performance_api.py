from __future__ import annotations

import asyncio
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.core.auth.dependencies import validate_dashboard_session
from app.core.crypto import TokenEncryptor
from app.db.models import Account, AccountStatus, RequestLog
from app.db.session import SessionLocal
from app.modules.reports.repository import ReportsRepository
from app.modules.reports.schemas import ReportsOptionsResponse
from app.modules.reports.service import ReportsService

pytestmark = pytest.mark.integration


async def test_concurrent_options_requests_share_cache_initialized_at_startup(async_client, app_instance, monkeypatch):
    caches = app_instance.state.reports_caches
    entered = asyncio.Event()
    release = asyncio.Event()

    async def compute(**kwargs):
        entered.set()
        await release.wait()
        return ReportsOptionsResponse(models=["m1"], useragents=[])

    compute_mock = AsyncMock(side_effect=compute)
    monkeypatch.setattr(ReportsService, "get_options", compute_mock)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        requests = [tasks.create_task(async_client.get("/api/reports/options")) for _ in range(10)]
        await entered.wait()
        release.set()
    assert app_instance.state.reports_caches is caches
    assert all(request.result().status_code == 200 for request in requests)
    assert all(request.result().json() == {"models": ["m1"], "useragents": []} for request in requests)
    compute_mock.assert_awaited_once()


async def test_options_are_scoped_without_full_report_or_speed_queries(async_client, db_setup, monkeypatch):
    import app.modules.reports.repository as module

    monkeypatch.setattr(module, "_daily_speed_medians_stmt", lambda *_: pytest.fail("options executed median SQL"))
    monkeypatch.setattr(ReportsRepository, "aggregate_summary", AsyncMock(side_effect=AssertionError("full report")))
    async with SessionLocal() as session:
        for model, group, key in [
            ("m1", "CLI", "key1"),
            ("m2", None, "key1"),
            ("m3", "SDK", "key2"),
            ("m4", " ", "key1"),
        ]:
            session.add(
                RequestLog(
                    request_id=model,
                    model=model,
                    status="success",
                    api_key_id=key,
                    useragent_group=group,
                    requested_at=datetime(2026, 6, 1),
                )
            )
        await session.commit()
    response = await async_client.get(
        "/api/reports/options", params={"start_date": "2026-06-01", "end_date": "2026-06-30", "api_key_id": "key1"}
    )
    assert response.status_code == 200
    assert response.json() == {"models": ["m1", "m2", "m4"], "useragents": ["CLI", "Missing User-Agent"]}


async def test_long_report_skips_speed_and_reuses_successful_cache(async_client, db_setup, monkeypatch):
    import app.modules.reports.repository as module

    monkeypatch.setattr(module, "_daily_speed_medians_stmt", lambda *_: pytest.fail("long report executed median SQL"))
    query = {"start_date": "2026-06-01", "end_date": "2026-08-29"}
    first = await async_client.get("/api/reports", params=query)
    assert first.status_code == 200
    assert first.json()["speedMetricsAvailable"] is False
    assert first.json()["speedMetricsMaxDays"] == 7
    monkeypatch.setattr(ReportsRepository, "aggregate_summary", AsyncMock(side_effect=AssertionError("cache miss")))
    second = await async_client.get("/api/reports", params=query)
    assert second.status_code == 200
    assert second.json() == first.json()


def _speed_median_log(rng: random.Random, index: int, requested_at: datetime) -> RequestLog:
    ttft = rng.choice([None, *range(40, 4000, 37)])
    return RequestLog(
        request_id=f"speed-median-{index}",
        requested_at=requested_at,
        account_id=rng.choice(["acc-a", "acc-b", None]),
        api_key_id=rng.choice(["key-a", "key-b", None]),
        model=rng.choice(["m1", "m2"]),
        useragent_group=rng.choice(["CLI", "SDK", None]),
        request_kind=rng.choice(["normal"] * 8 + ["warmup", "limit_warmup"]),
        source=rng.choice([None] * 8 + ["limit_warmup"]),
        status="success",
        output_tokens=rng.choice([None, 0, *range(1, 3000, 53)]),
        reasoning_tokens=rng.choice([None, 0, *range(1, 400, 29)]),
        latency_ms=rng.choice([None, *(None if ttft is None else ttft + extra for extra in (-5, 0, 1, 250, 9000))]),
        latency_first_token_ms=ttft,
        latency_queue_ms=rng.choice([None, *range(0, 500, 13)]),
    )


def _expected_speed_samples(
    logs: list[RequestLog],
    timezone_info: ZoneInfo,
    account_id: str | None = None,
    api_key_id: str | None = None,
    model: str | None = None,
    useragent_group: str | None = None,
) -> dict[str, tuple[list[float], list[float], list[float]]]:
    samples: dict[str, tuple[list[float], list[float], list[float]]] = defaultdict(lambda: ([], [], []))
    for log in logs:
        if log.source == "limit_warmup" or log.request_kind in ("warmup", "limit_warmup"):
            continue
        if (account_id and log.account_id != account_id) or (api_key_id and log.api_key_id != api_key_id):
            continue
        if model and log.model != model:
            continue
        if useragent_group == "Missing User-Agent":
            if log.useragent_group is not None:
                continue
        elif useragent_group and log.useragent_group != useragent_group:
            continue
        local_day = log.requested_at.replace(tzinfo=timezone.utc).astimezone(timezone_info).date().isoformat()
        ttft_values, tps_values, queue_values = samples[local_day]
        if log.latency_first_token_ms is not None:
            ttft_values.append(log.latency_first_token_ms)
        if log.latency_queue_ms is not None:
            queue_values.append(log.latency_queue_ms)
        tokens = None if log.output_tokens is None else log.output_tokens - (log.reasoning_tokens or 0)
        if (
            tokens is not None
            and tokens > 0
            and log.latency_ms is not None
            and log.latency_first_token_ms is not None
            and log.latency_ms > log.latency_first_token_ms
        ):
            tps_values.append(tokens * 1000.0 / (log.latency_ms - log.latency_first_token_ms))
    return samples


async def test_speed_medians_match_per_day_medians_across_filters_and_dst(async_client, db_setup):
    # Europe/Berlin leaves summer time on 2026-10-25, so that local day spans
    # 25 UTC hours. The window is 2026-10-23..2026-10-27 local time.
    berlin = ZoneInfo("Europe/Berlin")
    window_start = datetime(2026, 10, 22, 22)
    window_end = datetime(2026, 10, 27, 23)
    rng = random.Random(1901)
    logs: list[RequestLog] = []
    for index in range(900):
        offset = rng.random() * (window_end - window_start).total_seconds()
        logs.append(_speed_median_log(rng, index, window_start + timedelta(seconds=offset)))
    edges = [
        window_start - timedelta(microseconds=1),
        window_start,
        datetime(2026, 10, 24, 21, 59, 59),
        datetime(2026, 10, 24, 22),
        datetime(2026, 10, 25, 22, 30),
        datetime(2026, 10, 25, 23),
        window_end - timedelta(microseconds=1),
        window_end,
    ]
    # Boundary rows are always valid samples with distinct values and their own
    # model, so the "edge" filter shows any row bucketed into the wrong day or
    # wrongly kept or dropped at the window ends.
    for index, requested_at in enumerate(edges):
        logs.append(
            RequestLog(
                request_id=f"speed-median-edge-{index}",
                requested_at=requested_at,
                account_id="acc-a",
                api_key_id="key-a",
                model="edge",
                useragent_group=None,
                request_kind="normal",
                source=None,
                status="success",
                output_tokens=100 + 10 * index,
                latency_first_token_ms=100 * (index + 1),
                latency_ms=100 * (index + 1) + 2000 + 100 * index,
                latency_queue_ms=10 * (index + 1),
            )
        )
    encryptor = TokenEncryptor()
    async with SessionLocal() as session:
        session.add_all(
            Account(
                id=account_id,
                email=f"{account_id}@example.com",
                plan_type="plus",
                access_token_encrypted=encryptor.encrypt("access"),
                refresh_token_encrypted=encryptor.encrypt("refresh"),
                id_token_encrypted=encryptor.encrypt("id"),
                last_refresh=datetime(2026, 10, 1),
                status=AccountStatus.ACTIVE,
            )
            for account_id in ("acc-a", "acc-b")
        )
        await session.flush()
        session.add_all(logs)
        await session.commit()
    in_window = [log for log in logs if window_start <= log.requested_at < window_end]
    sample_counts: list[int] = []

    for filters in (
        {},
        {"account_id": "acc-a", "api_key_id": "key-b", "model": "m1"},
        {"useragent_group": "CLI", "model": "m2"},
        {"useragent_group": "Missing User-Agent"},
        {"model": "edge"},
    ):
        response = await async_client.get(
            "/api/reports",
            params={"start_date": "2026-10-23", "end_date": "2026-10-27", "timezone": "Europe/Berlin", **filters},
        )
        assert response.status_code == 200
        expected = _expected_speed_samples(in_window, berlin, **filters)
        daily = response.json()["daily"]
        assert [row["date"] for row in daily] == sorted(expected)
        for row in daily:
            sample_counts.extend(len(values) for values in expected[row["date"]])
            ttft, tps, queue = (median(values) if values else 0.0 for values in expected[row["date"]])
            assert row["medianTtftMs"] == pytest.approx(ttft, abs=0.006)
            assert row["medianTps"] == pytest.approx(tps, abs=0.006)
            assert row["medianQueueMs"] == pytest.approx(queue, abs=0.006)
    # The seeded data must exercise both median shapes.
    assert {count % 2 for count in sample_counts if count} == {0, 1}


async def test_options_validate_dates_and_require_dashboard_auth(async_client, app_instance):
    for query in [
        {"start_date": "2026-06-02", "end_date": "2026-06-01"},
        {"start_date": "2020-01-01", "end_date": "2026-01-01"},
    ]:
        response = await async_client.get("/api/reports/options", params=query)
        assert response.status_code == 400

    async def unauthorized():
        raise HTTPException(status_code=401)

    app_instance.dependency_overrides[validate_dashboard_session] = unauthorized
    assert (await async_client.get("/api/reports/options")).status_code == 401
