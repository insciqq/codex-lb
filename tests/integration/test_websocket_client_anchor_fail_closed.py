from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.integration.test_proxy_websocket_responses import _receive_until_terminal, _SequencedUpstreamWebSocket
from tests.integration.test_websocket_idle_loss import _FIRST, _create, _IdleSocketFixture, _previous_response_miss

pytestmark = [pytest.mark.integration, pytest.mark.timeout(20)]

_REPLY = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}
_NEXT = {"role": "user", "content": [{"type": "input_text", "text": "next"}]}


@pytest.mark.parametrize("path", ["/v1/responses", "/backend-api/codex/responses"])
def test_denied_client_anchor_keeps_telling_clients_to_drop_it(app_instance, monkeypatch, path):
    # Upstream denied the client's anchor once. A later frame presenting the
    # same anchor, on another socket, is not retried upstream within the
    # recovery window, and it gets the same instruction: resend without it.
    first = _SequencedUpstreamWebSocket([], deferred_message_batches=[[_previous_response_miss("resp_old")]])
    unused = _SequencedUpstreamWebSocket([])
    fixture = _IdleSocketFixture(monkeypatch, [first, unused])
    codes = []
    with TestClient(app_instance) as client:
        assert client.portal is not None
        client.portal.call(fixture.prepare)
        for _ in range(2):
            with client.websocket_connect(path, headers=fixture.headers()) as downstream:
                downstream.send_json(_create([_FIRST, _REPLY, _NEXT], parent="resp_old"))
                events, disconnect = _receive_until_terminal(downstream)
            assert disconnect is None
            terminal = events[-1]
            error = terminal.get("response", {}).get("error") or terminal.get("error")
            assert isinstance(error, dict)
            codes.append(error["code"])
            assert "resp_old" not in str(terminal)
    assert codes == ["previous_response_not_found", "previous_response_not_found"]
    assert len(first.sent_text) == 1
    assert unused.sent_text == []
