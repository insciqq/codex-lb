from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.integration.test_proxy_websocket_responses import _receive_until_terminal, _SequencedUpstreamWebSocket
from tests.integration.test_websocket_idle_loss import _PATH, _IdleSocketFixture

pytestmark = [pytest.mark.integration, pytest.mark.timeout(20)]


@pytest.mark.parametrize("anchored", [False, True])
def test_list_valued_input_item_type_is_rejected_before_routing(app_instance, monkeypatch, anchored):
    upstream = _SequencedUpstreamWebSocket([])
    fixture = _IdleSocketFixture(monkeypatch, [upstream])
    request = {
        "type": "response.create",
        "model": "gpt-5.4",
        "input": [{"type": ["tool_search_call"], "call_id": "c"}],
        "stream": True,
    }
    if anchored:
        request["previous_response_id"] = "resp_anchor"
    with TestClient(app_instance) as client:
        assert client.portal is not None
        client.portal.call(fixture.prepare)
        with client.websocket_connect(_PATH, headers=fixture.headers()) as downstream:
            downstream.send_json(request)
            events, disconnect = _receive_until_terminal(downstream)
    assert disconnect is None
    assert events[-1]["type"] in {"error", "response.failed"}
    assert "invalid_request_error" in str(events[-1])
    assert upstream.sent_text == []
