from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.core.clients.native_egress import NativeEgressTransportError
from app.core.clients.proxy_websocket import (
    NativeUpstreamWebSocket,
    UpstreamWebSocketMessage,
    WebsocketsUpstreamWebSocket,
)
from app.core.utils.sse import parse_sse_data_json
from app.dependencies import get_proxy_service_for_app
from app.modules.proxy import service as proxy_service
from app.modules.proxy._service.http_bridge import upstream_events as http_bridge_upstream_events
from tests.integration.test_direct_websocket_frameless_drop import (
    _DroppedConnection,
    _native_output,
    _NativeScript,
    _output_events,
)
from tests.integration.test_http_promotion_accounting import _key
from tests.integration.test_http_responses_bridge import (
    _cleanup_http_bridge_sessions,  # noqa: F401
    _PromotionUpstreamWebSocket,
)
from tests.integration.test_http_responses_bridge import (
    promotion_transport as promotion_transport,
)

pytestmark = pytest.mark.integration


class _AuthoredCloseUpstream(_PromotionUpstreamWebSocket):
    def __init__(self, close_code: int) -> None:
        super().__init__("resp_frameless")
        self.close_code = close_code

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)
        for event in _output_events():
            await self._messages.put(UpstreamWebSocketMessage("text", text=event))
        await self._messages.put(UpstreamWebSocketMessage("close", close_code=self.close_code))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ending", "penalized"),
    [("frameless", False), ("synthetic-1006", False), ("authored-1011", True), ("native-protocol-error", True)],
)
async def test_bridge_drop_after_output_penalizes_only_upstream_authored_closes(
    async_client, app_instance, promotion_transport, monkeypatch, ending: str, penalized: bool
) -> None:
    upstreams, raw_calls, _ = promotion_transport
    if ending == "frameless":
        dropped = _DroppedConnection(_output_events())
        upstream: Any = WebsocketsUpstreamWebSocket(cast(Any, dropped))
        sent_text = dropped.sent_text
    elif ending == "native-protocol-error":
        native = _NativeScript(
            [
                *_native_output(),
                NativeEgressTransportError("native websocket protocol failed", failure_phase="protocol"),
            ]
        )
        upstream = NativeUpstreamWebSocket(cast(Any, native))
        sent_text = native.sent_text
    else:
        upstream = _AuthoredCloseUpstream(1006 if ending == "synthetic-1006" else 1011)
        sent_text = upstream.sent_text
    connect = AsyncMock(side_effect=[upstream])
    monkeypatch.setattr(proxy_service, "connect_responses_websocket", connect)
    service = get_proxy_service_for_app(app_instance)
    health = AsyncMock()
    monkeypatch.setattr(service, "_handle_stream_error", health)
    drain_signals = AsyncMock()
    monkeypatch.setattr(http_bridge_upstream_events, "_record_http_bridge_account_timeout_signal", drain_signals)
    key = await _key(async_client, f"bridge-drop-{ending}")
    body = {
        "model": "gpt-5.4",
        "instructions": "test",
        "input": "hello",
        "stream": True,
        "prompt_cache_key": f"bridge-drop-{ending}",
    }

    response = await async_client.post("/v1/responses", json=body, headers={"Authorization": f"Bearer {key['key']}"})

    assert response.status_code == 200
    events = [e for block in response.text.split("\n\n") if (e := parse_sse_data_json(block)) is not None]
    assert [e["delta"] for e in events if e["type"] == "response.output_text.delta"] == ["partial"]
    failures = [e for e in events if e["type"] in {"error", "response.failed"}]
    assert len(failures) == 1
    detail = failures[0]["response"] if failures[0]["type"] == "response.failed" else failures[0]
    assert isinstance(detail, dict)
    error = detail["error"]
    assert isinstance(error, dict)
    assert error["code"] == "stream_incomplete"
    assert len(sent_text) == 1
    connect.assert_awaited_once()
    assert not raw_calls and not upstreams
    # A drop after output is not eventless, whatever its attribution.
    drain_signals.assert_not_awaited()
    if penalized:
        health.assert_awaited_once()
        assert health.await_args is not None
        assert health.await_args.args[2] == "stream_incomplete"
    else:
        health.assert_not_awaited()
    assert json.loads(sent_text[0])["type"] == "response.create"
