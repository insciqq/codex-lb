"""HTTP bridge transport choice with developer messages kept inline in ``input``."""

from __future__ import annotations

import json
from typing import cast

import pytest

import app.modules.proxy.service as proxy_module
from tests.integration.test_http_responses_bridge import (  # noqa: F401  (fixture)
    _collect_sse_events,
    _FakeBridgeUpstreamWebSocket,
    _import_account,
    _install_bridge_settings,
    _promotion_history,
    promotion_transport,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_developer_attachment_after_an_assistant_reply_still_promotes_history(
    async_client,
    promotion_transport,  # noqa: F811
):
    """The old hoist kept a developer file as a user item, which made the request conversation history."""
    upstreams, raw_calls, _ = promotion_transport
    history = _promotion_history()
    history[-1] = {
        "role": "developer",
        "content": [
            {"type": "input_text", "text": "Use these notes"},
            {"type": "input_file", "filename": "notes.txt", "file_data": "data:text/plain;base64,bm90ZXM="},
        ],
    }
    body = {"model": "gpt-5.4", "instructions": "test", "stream": True, "input": history}

    events = await _collect_sse_events(
        async_client, "/v1/responses", json_body=body, headers={"user-agent": "OpenAI/Python"}
    )

    assert events[-1]["type"] == "response.completed"
    assert not raw_calls
    assert len(upstreams) == 1


def _m(role: str, text: str, part_type: str = "input_text") -> dict[str, object]:
    return {"type": "message", "role": role, "content": [{"type": part_type, "text": text}]}


_DEV0 = _m("developer", "DEV0 sandbox=read-only")
_DEV0_CHANGED = _m("developer", "DEV0 sandbox=danger-full-access")
_DEV_NEW = _m("developer", "DEVNEW approval_policy=never")
_DEV_TRAIL = _m("developer", "DEVTRAIL plan-mode")
_ENV = _m("user", "<environment_context>cwd</environment_context>")
_U1 = _m("user", "first question")
_A1 = _m("assistant", "OK", "output_text")
_U2 = _m("user", "second question")
_U3 = _m("user", "third question")


# (stored turn, next full resend, what the anchored request sends)
def _mixed_developer(text: str) -> dict[str, object]:
    item = _m("developer", text)
    cast(list[object], item["content"]).append(
        {"type": "input_file", "filename": "notes.txt", "file_data": "data:text/plain;base64,bm90ZXM="}
    )
    return item


_FRENCH = _m("developer", "Use French.")
_ENGLISH = _m("developer", "Use English.")
_MIXED_ENGLISH = _mixed_developer("Use English")
_MIXED_FRENCH = _mixed_developer("only use French")

_INSERT_CASES = {
    "append-after-output": ([_DEV0, _ENV, _U1], [_DEV0, _ENV, _U1, _A1, _DEV_NEW, _U2], [_A1, _DEV_NEW, _U2]),
    "insert-inside-prefix": ([_DEV0, _ENV, _U1], [_DEV0, _DEV_NEW, _ENV, _U1, _A1, _U2], [_DEV_NEW, _A1, _U2]),
    "insert-between-prefix-items": ([_DEV0, _ENV, _U1], [_DEV0, _ENV, _DEV_NEW, _U1, _A1, _U2], [_DEV_NEW, _A1, _U2]),
    "changed-leading-developer": ([_DEV0, _ENV, _U1], [_DEV0_CHANGED, _ENV, _U1, _A1, _U2], [_DEV0_CHANGED, _A1, _U2]),
    "insert-right-after-prefix": ([_DEV0, _ENV, _U1], [_DEV0, _ENV, _U1, _DEV_NEW, _A1, _U2], [_DEV_NEW, _A1, _U2]),
    "trailing-developer-stored": ([_DEV0, _ENV, _U1, _DEV_TRAIL], [_DEV0, _ENV, _U1, _DEV_TRAIL, _A1, _U2], [_A1, _U2]),
    "removed-leading-developer": ([_DEV0, _ENV, _U1], [_ENV, _U1, _A1, _U2], [_A1, _U2]),
    # The old hoist kept the attachment as a user item and moved the text:
    # an unchanged attachment does not prove the changed text was sent.
    "changed-text-beside-unchanged-attachment": (
        [_MIXED_ENGLISH, _U1],
        [_MIXED_FRENCH, _U1, _A1, _U2],
        [_MIXED_FRENCH, _A1, _U2],
    ),
    # Each stored occurrence covers one occurrence: a repeat that reverses a
    # later instruction is new.
    "repeated-instruction-reverses-a-later-one": (
        [_FRENCH, _ENGLISH, _U1],
        [_FRENCH, _ENGLISH, _U1, _FRENCH, _A1, _U2],
        [_FRENCH, _A1, _U2],
    ),
}


async def _run_bridge_turns(
    async_client, monkeypatch, tag: str, turns: list[tuple[list[dict[str, object]], str | None]]
) -> list[_FakeBridgeUpstreamWebSocket]:
    """Send each ``(input, previous_response_id)`` turn on one warm HTTP bridge session."""
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_account(async_client, f"acc_dev_insert_{tag}", f"dev-insert-{tag}@example.com")
    upstreams: list[_FakeBridgeUpstreamWebSocket] = []

    async def fake_ensure_fresh_with_budget(self, account, *, force=False, timeout_seconds):
        del self, force, timeout_seconds
        return account

    async def fake_connect_responses_websocket(
        headers, access_token, account_id_header, *, base_url=None, session=None
    ):
        del headers, access_token, account_id_header, base_url, session
        upstream = _FakeBridgeUpstreamWebSocket(f"resp_conn{len(upstreams) + 1}")
        upstreams.append(upstream)
        return upstream

    monkeypatch.setattr(proxy_module.ProxyService, "_ensure_fresh_with_budget", fake_ensure_fresh_with_budget)
    monkeypatch.setattr(proxy_module, "connect_responses_websocket", fake_connect_responses_websocket)
    headers = {"session_id": f"dev-insert-session-{tag}"}
    body = {"model": "gpt-5.1", "instructions": "You are Codex.", "stream": True}
    for turn_input, previous_response_id in turns:
        json_body: dict[str, object] = {**body, "input": turn_input}
        if previous_response_id is not None:
            json_body["previous_response_id"] = previous_response_id
        events = await _collect_sse_events(
            async_client, "/backend-api/codex/responses", json_body=json_body, headers=headers
        )
        assert events[-1]["type"] == "response.completed"
    return upstreams


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_INSERT_CASES))
async def test_warm_bridge_forwards_developer_messages_the_anchor_does_not_carry(
    async_client, app_instance, monkeypatch, case
):
    """The anchor carries what the stored turn sent; a new or changed developer message still goes upstream.

    Before developer messages stayed inline, the current developer text reached
    every request through ``instructions``. A message the stored turn already
    sent is not repeated, and nothing is repeated on the turn after.
    """
    del app_instance
    stored, resend, expected = _INSERT_CASES[case]
    third = [*resend, _A1, _U3]
    upstreams = await _run_bridge_turns(
        async_client, monkeypatch, case.replace("-", "_"), [(stored, None), (resend, None), (third, None)]
    )

    assert len(upstreams) == 1
    frames = [json.loads(text) for text in upstreams[0].sent_text]
    assert [frame.get("previous_response_id") for frame in frames] == [None, "resp_conn1_1", "resp_conn1_2"]
    assert frames[1]["input"] == expected
    assert frames[2]["input"] == [_A1, _U3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turns", "missing"),
    [
        # The client branches from the first response; only the second one
        # received the new instruction.
        pytest.param(
            [([_U1], None), ([_U1, _DEV_NEW, _A1, _U2], None), ([_U1, _DEV_NEW, _A1, _U2, _A1, _U3], "resp_conn1_1")],
            "DEVNEW",
            id="branch-from-an-older-response",
        ),
        # A developer-only turn has no stored prefix of its own: the first
        # turn's developer record does not describe its response.
        pytest.param(
            [([_DEV0, _U1], None), ([_DEV_NEW], None), ([_DEV0, _U1, _A1, _U3], None)],
            "DEV0",
            id="after-a-developer-only-turn",
        ),
    ],
)
async def test_warm_bridge_judges_developer_messages_against_the_anchored_response(
    async_client, app_instance, monkeypatch, turns, missing
):
    """A developer record trims a request only when it belongs to the response that request is anchored to."""
    del app_instance
    upstreams = await _run_bridge_turns(async_client, monkeypatch, f"anchored_record_{missing.lower()}", turns)

    assert len(upstreams) == 1
    frames = [json.loads(text) for text in upstreams[0].sent_text]
    assert len(frames) == 3
    assert missing in json.dumps(frames[2])
