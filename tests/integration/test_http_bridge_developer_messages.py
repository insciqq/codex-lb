"""HTTP bridge transport choice with developer messages kept inline in ``input``."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

import pytest

import app.modules.proxy.service as proxy_module
from app.dependencies import get_proxy_service_for_app
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


def _collaboration_mode(mode: str) -> dict[str, object]:
    return _m("developer", f"<collaboration_mode># {mode} Mode</collaboration_mode>")


def _baseline_developer(skills: str) -> dict[str, object]:
    """A captured-style two-part developer message: the Default mode and the skills list."""
    item = _collaboration_mode("Default")
    cast(list[object], item["content"]).append({"type": "input_text", "text": skills})
    return item


_BASELINE_OLD = _baseline_developer("Skills: testing")
_BASELINE_NEW = _baseline_developer("Skills: testing, linting")
_DEFAULT = _collaboration_mode("Default")
_PLAN = _collaboration_mode("Plan")
_PLAN_HISTORY = [_ENV, _U1, _A1, _PLAN, _U2]

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
    # Refreshing an earlier developer message must not make it the last one:
    # every later developer message of the range follows it again.
    "skills-refresh": (
        [_BASELINE_OLD, *_PLAN_HISTORY],
        [_BASELINE_NEW, *_PLAN_HISTORY, _A1, _U3],
        [_BASELINE_NEW, _PLAN, _A1, _U3],
    ),
    # The record is a sequence: the same messages in another order are a
    # change, and the client's last one must be the last one upstream.
    "reordered-developer-messages": (
        [_DEFAULT, _PLAN, _U1],
        [_PLAN, _DEFAULT, _U1, _A1, _U2],
        [_PLAN, _DEFAULT, _A1, _U2],
    ),
}


async def _run_bridge_turns(
    async_client,
    monkeypatch,
    tag: str,
    turns: list[tuple[list[dict[str, object]], str | None]],
    *,
    body: dict[str, object] | None = None,
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
    if body is None:
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


_MODE_PATTERN = re.compile(r"<collaboration_mode># (\w+) Mode</collaboration_mode>")


def _last_collaboration_mode(items: list[dict[str, object]]) -> str | None:
    modes = [
        mode
        for item in items
        if item.get("role") == "developer"
        for mode in _MODE_PATTERN.findall(json.dumps(item.get("content")))
    ]
    return modes[-1] if modes else None


_U4 = _m("user", "fourth question")
_U5 = _m("user", "fifth question")
_MODE_ROUND_TRIP_TURNS = [
    [_BASELINE_OLD, _ENV, _U1],
    [_BASELINE_OLD, _ENV, _U1, _A1, _PLAN, _U2],
    [_BASELINE_NEW, _ENV, _U1, _A1, _PLAN, _U2, _A1, _U3],
    [_BASELINE_NEW, _ENV, _U1, _A1, _PLAN, _U2, _A1, _U3, _A1, _DEFAULT, _U4],
    [_BASELINE_NEW, _ENV, _U1, _A1, _PLAN, _U2, _A1, _U3, _A1, _DEFAULT, _U4, _A1, _U5],
]
# What each anchored frame sends: the appended Plan, the refreshed message
# followed by Plan again, the appended Default, and nothing repeated after.
_MODE_ROUND_TRIP_FRAMES = [
    [_BASELINE_OLD, _ENV, _U1],
    [_A1, _PLAN, _U2],
    [_BASELINE_NEW, _PLAN, _A1, _U3],
    [_A1, _DEFAULT, _U4],
    [_A1, _U5],
]


@pytest.mark.asyncio
async def test_warm_bridge_keeps_the_clients_last_collaboration_mode(async_client, app_instance, monkeypatch):
    """Default, then Plan, a skills refresh, then Default again: the chain upstream ends with the client's mode."""
    del app_instance
    upstreams = await _run_bridge_turns(
        async_client, monkeypatch, "mode_round_trip", [(turn, None) for turn in _MODE_ROUND_TRIP_TURNS]
    )

    assert len(upstreams) == 1
    frames = [json.loads(text) for text in upstreams[0].sent_text]
    assert [frame.get("previous_response_id") for frame in frames] == [
        None,
        *(f"resp_conn1_{index}" for index in range(1, len(frames))),
    ]
    assert [frame["input"] for frame in frames] == _MODE_ROUND_TRIP_FRAMES
    chain: list[dict[str, object]] = []
    for frame, client_input in zip(frames, _MODE_ROUND_TRIP_TURNS, strict=True):
        chain.extend(frame["input"])
        assert _last_collaboration_mode(chain) == _last_collaboration_mode(client_input)


_SKILLS_V1 = _m("developer", "Skills list version 1")
_SKILLS_V2 = _m("developer", "Skills list version 2")
_DEFAULT_JSON = _collaboration_mode("Default")
cast(list[object], _DEFAULT_JSON["content"]).append({"type": "input_text", "text": "Reply in JSON."})


@pytest.mark.asyncio
async def test_json_suffix_order(async_client, app_instance, monkeypatch):
    """JSON mode: a skills refresh resends the later modes too, including the one the old hoist kept inline.

    The old hoist left the Default-mode message (it mentions JSON) in
    ``input``, so it is stored history the anchor carries; once the refreshed
    skills list and Plan are sent again, Default must follow them.
    """
    del app_instance
    stored = [_SKILLS_V1, _PLAN, _DEFAULT_JSON, _U1]
    resend = [_SKILLS_V2, _PLAN, _DEFAULT_JSON, _U1, _A1, _U2]
    body: dict[str, object] = {
        "model": "gpt-5.1",
        "instructions": "You are Codex.",
        "stream": True,
        "text": {"format": {"type": "json_object"}},
    }
    upstreams = await _run_bridge_turns(
        async_client,
        monkeypatch,
        "json_suffix_order",
        [(stored, None), (resend, None), ([*resend, _A1, _U3], None)],
        body=body,
    )

    assert len(upstreams) == 1
    frames = [json.loads(text) for text in upstreams[0].sent_text]
    assert [frame.get("previous_response_id") for frame in frames] == [None, "resp_conn1_1", "resp_conn1_2"]
    assert frames[1]["input"] == [_SKILLS_V2, _PLAN, _DEFAULT_JSON, _A1, _U2]
    assert frames[2]["input"] == [_A1, _U3]
    assert _last_collaboration_mode([*frames[0]["input"], *frames[1]["input"]]) == "Default"


def _with_id(item: dict[str, object], item_id: str) -> dict[str, object]:
    return {**item, "id": item_id}


def _without_id(item: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in item.items() if key != "id"}


# (stored turn, next full resend, the developer messages the anchored request
# resends, JSON mode). Codex gives every input item a client id.
_RESENT_ID_CASES = {
    # The old hoist kept the JSON-mentioning Default message (and its id) in
    # ``input``; the skills refresh resends it after Plan.
    "json-kept-developer": (
        [_with_id(_SKILLS_V1, "msg_skills"), _with_id(_PLAN, "msg_plan"), _with_id(_DEFAULT_JSON, "msg_mode"), _U1],
        [_with_id(_SKILLS_V2, "msg_skills"), _with_id(_PLAN, "msg_plan"), _with_id(_DEFAULT_JSON, "msg_mode"), _U1],
        [_SKILLS_V2, _PLAN, _DEFAULT_JSON],
        True,
    ),
    # The old hoist kept the attachment (and the id) as a user item; the
    # refresh before it resends the whole message.
    "developer-with-attachment": (
        [_with_id(_SKILLS_V1, "msg_skills"), _with_id(_MIXED_ENGLISH, "msg_notes"), _U1],
        [_with_id(_SKILLS_V2, "msg_skills"), _with_id(_MIXED_ENGLISH, "msg_notes"), _U1],
        [_SKILLS_V2, _MIXED_ENGLISH],
        False,
    ),
    "changed-text-beside-an-attachment": (
        [_with_id(_MIXED_ENGLISH, "msg_notes"), _U1],
        [_with_id(_MIXED_FRENCH, "msg_notes"), _U1],
        [_MIXED_FRENCH],
        False,
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_RESENT_ID_CASES))
async def test_warm_bridge_resends_developer_messages_without_their_item_ids(
    async_client, app_instance, monkeypatch, case
):
    """A developer message the anchored request sends again goes without its client item id.

    The anchored chain already holds the stored turn's items with their ids,
    so the same id again would be a duplicate item. The record keeps the
    client's items, so the turn after sends no developer message again.
    """
    del app_instance
    stored, resend_prefix, expected_developer, json_mode = _RESENT_ID_CASES[case]
    resend = [*resend_prefix, _A1, _U2]
    body: dict[str, object] = {"model": "gpt-5.1", "instructions": "You are Codex.", "stream": True}
    if json_mode:
        body["text"] = {"format": {"type": "json_object"}}
    upstreams = await _run_bridge_turns(
        async_client,
        monkeypatch,
        f"resent_ids_{case.replace('-', '_')}",
        [(stored, None), (resend, None), ([*resend, _A1, _U3], None)],
        body=body,
    )

    assert len(upstreams) == 1
    frames = [json.loads(text) for text in upstreams[0].sent_text]
    assert [frame.get("previous_response_id") for frame in frames] == [None, "resp_conn1_1", "resp_conn1_2"]
    assert any("id" in item for item in frames[0]["input"])
    assert frames[1]["input"] == [*(_without_id(item) for item in expected_developer), _A1, _U2]
    assert frames[2]["input"] == [_A1, _U3]


_NATIVE_CODEX_HEADERS = {
    "session_id": "dev-native-only-session",
    "thread-id": "dev-native-only-thread",
    "originator": "Codex Desktop",
    "user-agent": "Codex Desktop/0.130.0 (Mac OS 26.1.0; arm64)",
}


async def _close_local_bridge_sessions(app_instance) -> None:
    service = get_proxy_service_for_app(app_instance)
    async with service._http_bridge_lock:
        sessions = list(service._http_bridge_sessions.values())
        service._http_bridge_sessions.clear()
        service._http_bridge_turn_state_index.clear()
        service._http_bridge_previous_response_index.clear()
    for session in sessions:
        await service._close_http_bridge_session(session)


@pytest.mark.asyncio
async def test_native_developer_only_turn_after_a_closed_session_keeps_the_durable_anchor(
    async_client, app_instance, monkeypatch
):
    """Developer messages alone are no native Codex history; such a body stays a delta.

    The old hoist left a developer-only body with an empty ``input``, which was
    never a complete native history, so the bridge sent it with the thread's
    durable anchor. Judging the wire list would send the developer messages
    unanchored, without the conversation they belong to.
    """
    _install_bridge_settings(monkeypatch, enabled=True)
    await _import_account(async_client, "acc_dev_native_only", "dev-native-only@example.com")
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
    body: dict[str, object] = {"model": "gpt-5.1", "instructions": "You are Codex.", "stream": True}

    first = await _collect_sse_events(
        async_client, "/backend-api/codex/responses", json_body={**body, "input": [_U1]}, headers=_NATIVE_CODEX_HEADERS
    )
    assert first[-1]["type"] == "response.completed"
    await _close_local_bridge_sessions(app_instance)
    second = await _collect_sse_events(
        async_client,
        "/backend-api/codex/responses",
        json_body={**body, "input": [_DEV_NEW]},
        headers=_NATIVE_CODEX_HEADERS,
    )

    assert second[-1]["type"] == "response.completed"
    assert len(upstreams) == 2
    [frame] = [json.loads(text) for text in upstreams[1].sent_text]
    assert frame["previous_response_id"] == "resp_conn1_1"
    assert frame["input"] == [_DEV_NEW]


_CODEX_BODIES = sorted(
    path for path in Path("tests/fixtures/codex_bodies").glob("*.json") if path.name != "provenance.json"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _CODEX_BODIES, ids=lambda path: path.stem)
async def test_warm_bridge_resends_no_developer_message_on_an_append_only_codex_thread(
    async_client, app_instance, monkeypatch, path
):
    """Real Codex bodies: once the thread is anchored, an append-only history sends no developer message again."""
    del app_instance
    body = json.loads(path.read_text())
    turns = [list(body.pop("input"))]
    for number in range(3):
        turns.append([*turns[-1], _A1, _m("user", f"follow-up {number}")])

    upstreams = await _run_bridge_turns(
        async_client, monkeypatch, f"steady_{path.stem}", [(turn, None) for turn in turns], body=body
    )

    assert len(upstreams) == 1
    frames = [json.loads(text) for text in upstreams[0].sent_text]
    assert [frame.get("previous_response_id") for frame in frames] == [
        None,
        "resp_conn1_1",
        "resp_conn1_2",
        "resp_conn1_3",
    ]
    assert [frame["input"] for frame in frames[1:]] == [[_A1, turn[-1]] for turn in turns[1:]]
