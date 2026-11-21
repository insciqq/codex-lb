from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.modules.proxy.api as proxy_api
import app.modules.proxy.rate_limit_events as rate_limit_events
import app.modules.proxy.service as proxy_service
from app.core.clients.proxy_websocket import UpstreamWebSocketMessage
from app.core.config.dashboard_overrides import with_dashboard_overrides
from app.core.crypto import TokenEncryptor
from app.core.usage.live_hub import register_live_usage_publisher
from app.core.utils.time import utcnow
from app.db.models import Account, AccountStatus, UsageHistory
from app.db.session import SessionLocal
from app.dependencies import get_proxy_service_for_app
from app.modules.proxy.http_bridge_forwarding import HTTPBridgeForwardContext, build_owner_forward_headers
from app.modules.proxy.load_balancer import AccountSelection

pytestmark = pytest.mark.integration


@dataclass
class QuotaPool:
    account: Account
    key: str
    reset_at: int


@pytest_asyncio.fixture
async def quota_pool(async_client):
    now = utcnow()
    reset_at = int((now + timedelta(days=3)).timestamp())
    encryptor = TokenEncryptor()
    async with SessionLocal() as session:
        accounts = []
        for index, used in enumerate((97, 36)):
            account = Account(
                id=f"quota-pool-{index}",
                chatgpt_account_id=f"upstream-quota-{index}",
                email=f"quota-{index}@example.com",
                plan_type="pro",
                access_token_encrypted=encryptor.encrypt("access"),
                refresh_token_encrypted=encryptor.encrypt("refresh"),
                id_token_encrypted=encryptor.encrypt("id"),
                last_refresh=now,
                status=AccountStatus.ACTIVE,
            )
            session.add(account)
            accounts.append(account)
            session.add(
                UsageHistory(
                    account_id=account.id,
                    recorded_at=now,
                    window="primary",
                    used_percent=used,
                    window_minutes=10080,
                    reset_at=reset_at + index * 3600,
                    credits_has=False,
                    credits_unlimited=False,
                    credits_balance=0,
                )
            )
        await session.commit()
        for account in accounts:
            session.expunge(account)
    response = await async_client.post("/api/api-keys", json={"name": "pool-quota-test"})
    assert response.status_code == 200, response.text
    return QuotaPool(accounts[0], response.json()["key"], reset_at)


def _upstream_events():
    quota = {
        "type": "codex.rate_limits",
        "plan_type": "pro",
        "account_id": "must-not-leak",
        "rate_limits": {"primary": {"used_percent": 97, "window_minutes": 10080, "reset_at": 1900000000}},
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    }
    return [
        quota,
        {**quota, "metered_limit_name": "codex_other"},
        {"type": "response.created", "response": {"id": "resp_quota", "status": "in_progress"}},
        {"type": "response.output_text.delta", "response_id": "resp_quota", "delta": "OK"},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_quota",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            },
        },
    ]


class QuotaUpstream:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.closed = False

    async def send_text(self, text):
        for event in _upstream_events():
            self.messages.put_nowait(UpstreamWebSocketMessage(kind="text", text=json.dumps(event)))

    async def receive(self):
        return await self.messages.get()

    async def close(self):
        self.closed = True


def _assert_downstream(events, *, hidden, reset_at, pool="windows"):
    quotas = [event for event in events if event["type"] == "codex.rate_limits"]
    upstream_quotas = [event for event in _upstream_events() if event["type"] == "codex.rate_limits"]
    if hidden:
        assert quotas == []
    elif pool == "monthly_only":
        # The pool has no primary/secondary window to rebuild the events
        # from; a client that may see quota gets them unchanged.
        assert quotas == upstream_quotas
    else:
        assert len(quotas) == 2
        assert quotas[0]["rate_limits"] == {
            "primary": None,
            "secondary": {"used_percent": 66.5, "window_minutes": 10080, "reset_at": reset_at},
        }
        assert "plan_type" not in quotas[0]
        assert "account_id" not in quotas[0]
        # Another limit family is not relabelled as pool usage; it passes through.
        assert quotas[1] == upstream_quotas[1]
    assert [event["type"] for event in events if event["type"] != "codex.rate_limits"] == [
        "response.created",
        "response.output_text.delta",
        "response.completed",
    ]
    assert next(event for event in events if event["type"] == "response.output_text.delta")["delta"] == "OK"


def _sse_events(text):
    events = [
        json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"
    ]
    return [event for event in events if event["type"] != "codex.keepalive"]


async def _monthly_only_pool(self):
    return {"x-codex-monthly-used-percent": "40", "x-codex-monthly-window-minutes": "43200"}


@pytest.mark.parametrize(
    ("transport", "variant"),
    [("http", "typed"), ("bridge", "typed"), ("http", "monthly_only"), ("bridge", "monthly_only")],
)
@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("path", ["/backend-api/codex/responses", "/backend-api/codex/responses/"])
async def test_native_sse_projects_pool_and_honors_privacy(
    async_client,
    app_instance,
    monkeypatch,
    quota_pool,
    transport,
    variant,
    hidden,
    path,
):
    settings = await async_client.put(
        "/api/settings",
        json={
            "hideUpstreamQuotaFromApiKeys": hidden,
            "apiKeyAuthEnabled": True,
            "httpDownstreamTransportPolicy": "always_websocket" if transport == "bridge" else "always_http",
        },
    )
    assert settings.status_code == 200, settings.text
    app_settings = proxy_api.get_settings().model_copy(
        update={"http_responses_session_bridge_enabled": transport == "bridge"}
    )
    monkeypatch.setattr(proxy_api, "get_settings", lambda: with_dashboard_overrides(app_settings))
    monkeypatch.setattr(proxy_service, "get_settings", lambda: with_dashboard_overrides(app_settings))
    upstream = QuotaUpstream()
    ingested = []
    register_live_usage_publisher(lambda snapshot, **identity: ingested.append((snapshot, identity)))

    async def select_account(self, *args, **kwargs):
        return AccountSelection(account=quota_pool.account, error_message=None, error_code=None)

    async def fresh(self, account, **kwargs):
        return account

    async def connect(*args, **kwargs):
        assert transport == "bridge"
        return upstream

    async def stream(*args, **kwargs):
        assert transport == "http"
        for event in _upstream_events():
            yield "data: " + json.dumps(event) + "\n\n"

    monkeypatch.setattr(proxy_service.ProxyService, "_select_account_with_budget", select_account)
    monkeypatch.setattr(proxy_service.ProxyService, "_ensure_fresh_with_budget", fresh)
    monkeypatch.setattr(proxy_service, "connect_responses_websocket", connect)
    monkeypatch.setattr(proxy_service, "core_stream_responses", stream)
    if variant == "monthly_only":
        monkeypatch.setattr(proxy_service.ProxyService, "rate_limit_headers", _monthly_only_pool)
    service = get_proxy_service_for_app(app_instance)
    try:
        response = await async_client.post(
            path,
            headers={
                "Authorization": f"Bearer {quota_pool.key}",
                "user-agent": "codex-cli/0.154.0",
            },
            json={"model": "gpt-5.6-sol", "instructions": "", "input": "hi", "stream": True},
        )
        assert response.status_code == 200, response.text
        events = _sse_events(response.text)
        expected_pool = "monthly_only" if variant == "monthly_only" else "windows"
        _assert_downstream(events, hidden=hidden, reset_at=quota_pool.reset_at, pool=expected_pool)
        if hidden:
            assert "must-not-leak" not in response.text
            assert not any(name.startswith("x-codex-secondary-") for name in response.headers)
        elif variant != "monthly_only":
            assert float(response.headers["x-codex-secondary-used-percent"]) == 66.5
        if transport == "bridge":
            assert any(
                snapshot.primary.used_percent == 97 and identity["account_id"] == quota_pool.account.id
                for snapshot, identity in ingested
            )
        async with SessionLocal() as session:
            usage = (
                (await session.execute(select(UsageHistory).where(UsageHistory.account_id == quota_pool.account.id)))
                .scalars()
                .all()
            )
            assert all(row.used_percent == 97 for row in usage)
    finally:
        register_live_usage_publisher(None)
        for session in list(service._http_bridge_sessions.values()):
            await service._close_http_bridge_session(session)


async def test_native_sse_reads_quota_visibility_once_before_startup(
    async_client,
    monkeypatch,
    quota_pool,
):
    # Headers and quota events share one visibility snapshot taken before the
    # startup probe; a later settings read could fail after the probe started
    # and leave it running, or disagree with the headers after a toggle.
    settings = await async_client.put(
        "/api/settings",
        json={
            "hideUpstreamQuotaFromApiKeys": False,
            "apiKeyAuthEnabled": True,
            "httpDownstreamTransportPolicy": "always_http",
        },
    )
    assert settings.status_code == 200, settings.text
    real_cache = rate_limit_events.get_settings_cache()
    reads = 0

    async def get_once():
        nonlocal reads
        reads += 1
        if reads > 1:
            raise RuntimeError("settings cache refresh failed")
        return await real_cache.get()

    monkeypatch.setattr(rate_limit_events, "get_settings_cache", lambda: SimpleNamespace(get=get_once))

    async def select_account(self, *args, **kwargs):
        return AccountSelection(account=quota_pool.account, error_message=None, error_code=None)

    async def fresh(self, account, **kwargs):
        return account

    async def stream(*args, **kwargs):
        for event in _upstream_events():
            yield "data: " + json.dumps(event) + "\n\n"

    monkeypatch.setattr(proxy_service.ProxyService, "_select_account_with_budget", select_account)
    monkeypatch.setattr(proxy_service.ProxyService, "_ensure_fresh_with_budget", fresh)
    monkeypatch.setattr(proxy_service, "core_stream_responses", stream)

    response = await async_client.post(
        "/backend-api/codex/responses",
        headers={"Authorization": f"Bearer {quota_pool.key}", "user-agent": "codex-cli/0.154.0"},
        json={"model": "gpt-5.6-sol", "instructions": "", "input": "hi", "stream": True},
    )

    assert response.status_code == 200, response.text
    assert reads == 1
    assert float(response.headers["x-codex-secondary-used-percent"]) == 66.5
    _assert_downstream(_sse_events(response.text), hidden=False, reset_at=quota_pool.reset_at)


async def test_owner_forward_hop_relays_upstream_quota_event_to_origin(
    async_client,
    app_instance,
    monkeypatch,
    quota_pool,
):
    # The owner serving a forwarded native request has no quota headers of its
    # own; it must relay the upstream event so the origin can project it.
    settings = await async_client.put(
        "/api/settings",
        json={"httpDownstreamTransportPolicy": "always_websocket"},
    )
    assert settings.status_code == 200, settings.text
    app_settings = proxy_api.get_settings().model_copy(update={"http_responses_session_bridge_enabled": True})
    monkeypatch.setattr(proxy_api, "get_settings", lambda: with_dashboard_overrides(app_settings))
    monkeypatch.setattr(proxy_service, "get_settings", lambda: with_dashboard_overrides(app_settings))
    upstream = QuotaUpstream()

    async def select_account(self, *args, **kwargs):
        return AccountSelection(account=quota_pool.account, error_message=None, error_code=None)

    async def fresh(self, account, **kwargs):
        return account

    async def connect(*args, **kwargs):
        return upstream

    monkeypatch.setattr(proxy_service.ProxyService, "_select_account_with_budget", select_account)
    monkeypatch.setattr(proxy_service.ProxyService, "_ensure_fresh_with_budget", fresh)
    monkeypatch.setattr(proxy_service, "connect_responses_websocket", connect)
    payload = proxy_service.ResponsesRequest(model="gpt-5.6-sol", instructions="", input="hi", stream=True)
    forward_headers = build_owner_forward_headers(
        headers={"user-agent": "codex-cli/0.154.0", "originator": "codex_cli_rs"},
        payload=payload,
        context=HTTPBridgeForwardContext(
            origin_instance="origin-instance",
            target_instance=app_settings.http_responses_session_bridge_instance_id,
            codex_session_affinity=True,
            downstream_turn_state="turn_forward_quota",
        ),
    )
    service = get_proxy_service_for_app(app_instance)
    try:
        response = await async_client.post(
            "/internal/bridge/responses",
            headers=forward_headers,
            json=payload.model_dump_for_forwarding(),
        )
        assert response.status_code == 200, response.text
        events = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        quotas = [event for event in events if event["type"] == "codex.rate_limits"]
        assert quotas == [event for event in _upstream_events() if event["type"] == "codex.rate_limits"]
        assert not any(name.startswith("x-codex-secondary-") for name in response.headers)
        assert events[-1]["type"] == "response.completed"
    finally:
        for session in list(service._http_bridge_sessions.values()):
            await service._close_http_bridge_session(session)


@pytest.mark.parametrize("path", ["/backend-api/codex/responses", "/v1/responses"])
@pytest.mark.parametrize("mode", ["visible", "hidden", "unavailable", "monthly_only"])
async def test_websocket_projects_pool_without_interrupting_responses(
    async_client,
    app_instance,
    monkeypatch,
    quota_pool,
    path,
    mode,
):
    settings = await async_client.put(
        "/api/settings", json={"hideUpstreamQuotaFromApiKeys": mode == "hidden", "apiKeyAuthEnabled": True}
    )
    assert settings.status_code == 200, settings.text
    upstream = QuotaUpstream()

    async def connect(self, *args, **kwargs):
        return quota_pool.account, upstream

    async def unavailable(self):
        raise RuntimeError("quota cache unavailable")

    monkeypatch.setattr(proxy_service.ProxyService, "_connect_proxy_websocket", connect)
    if mode == "unavailable":
        monkeypatch.setattr(proxy_service.ProxyService, "rate_limit_headers", unavailable)
    if mode == "monthly_only":
        monkeypatch.setattr(proxy_service.ProxyService, "rate_limit_headers", _monthly_only_pool)

    def run_client():
        with TestClient(app_instance) as client:
            with client.websocket_connect(path, headers={"Authorization": f"Bearer {quota_pool.key}"}) as socket:
                socket.send_json({"type": "response.create", "model": "gpt-5.6-sol", "input": "hi"})
                events = []
                while not events or events[-1]["type"] != "response.completed":
                    events.append(socket.receive_json())
                return events

    events = await asyncio.to_thread(run_client)
    _assert_downstream(
        events,
        hidden=mode in {"hidden", "unavailable"},
        reset_at=quota_pool.reset_at,
        pool="monthly_only" if mode == "monthly_only" else "windows",
    )
