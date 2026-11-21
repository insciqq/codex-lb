"""WebSocket continuity with developer messages kept inline in ``input``.

Developer messages used to be hoisted into ``instructions`` before the proxy
classified a request; they now stay in ``input`` (upstream issue 2563), and the
replay heuristics must still judge conversation history without them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi.testclient import TestClient

import app.modules.proxy.api as proxy_api_module
import app.modules.proxy.service as proxy_module
from tests.integration.test_proxy_websocket_responses import (
    _FakeUpstreamMessage,
    _SequencedUpstreamWebSocket,
    _websocket_settings,
)

pytestmark = pytest.mark.integration

_PREWARM_TURN_METADATA = json.dumps({"request_kind": "prewarm", "turn_id": "turn_prewarm"})


def _message(role: str, text: str) -> dict[str, object]:
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}


def _response_batch(response_id: str, *, output_tokens: int = 1) -> list[_FakeUpstreamMessage]:
    return [
        _FakeUpstreamMessage(
            "text",
            text=json.dumps({"type": "response.created", "response": {"id": response_id, "status": "in_progress"}}),
        ),
        _FakeUpstreamMessage(
            "text",
            text=json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": response_id,
                        "status": "completed",
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": output_tokens,
                            "total_tokens": 1 + output_tokens,
                        },
                    },
                }
            ),
        ),
    ]


def _previous_response_not_found(response_id: str) -> list[_FakeUpstreamMessage]:
    return [
        _FakeUpstreamMessage(
            "text",
            text=json.dumps(
                {
                    "type": "error",
                    "status": 400,
                    "error": {
                        "type": "invalid_request_error",
                        "code": "previous_response_not_found",
                        "message": f"Previous response with id '{response_id}' not found.",
                        "param": "previous_response_id",
                    },
                }
            ),
        )
    ]


def _install_upstreams(monkeypatch: pytest.MonkeyPatch, upstreams: list[_SequencedUpstreamWebSocket]) -> list[int]:
    connects = [0]

    class _FakeSettingsCache:
        async def get(self):
            return _websocket_settings()

    async def allow_firewall(_websocket):
        return None

    async def allow_proxy_api_key(_authorization: str | None, *, request: object | None = None):
        return None

    async def fake_connect_proxy_websocket(self, headers, **kwargs):
        del self, headers, kwargs
        upstream = upstreams[min(connects[0], len(upstreams) - 1)]
        connects[0] += 1
        return SimpleNamespace(id="acct_ws_developer_messages"), upstream

    async def fake_write_request_log(self, **kwargs):
        del self, kwargs

    monkeypatch.setattr(proxy_api_module, "_websocket_firewall_denial_response", allow_firewall)
    monkeypatch.setattr(proxy_api_module, "validate_proxy_api_key_authorization", allow_proxy_api_key)
    monkeypatch.setattr(proxy_module, "get_settings_cache", lambda: _FakeSettingsCache())
    monkeypatch.setattr(proxy_module.ProxyService, "_connect_proxy_websocket", fake_connect_proxy_websocket)
    monkeypatch.setattr(proxy_module.ProxyService, "_write_request_log", fake_write_request_log)
    return connects


def _prewarm_frame() -> dict[str, object]:
    return {
        "type": "response.create",
        "model": "gpt-5.6-sol",
        "instructions": "",
        "input": [
            {"type": "additional_tools", "role": "developer", "tools": [{"type": "custom", "name": "shell"}]},
            _message("developer", "base instructions"),
        ],
        "generate": False,
        "stream": True,
    }


def _connection_headers(session_id: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer external-token",
        "chatgpt-account-id": "external-account",
        "session_id": session_id,
        "openai-beta": "responses_websockets=2026-02-06",
        "x-codex-turn-metadata": _PREWARM_TURN_METADATA,
    }


def test_chained_turn_start_after_prewarm_keeps_developer_messages_in_input(app_instance, monkeypatch):
    """``previous_response_id`` carries ``input`` forward but not ``instructions``."""
    upstream = _SequencedUpstreamWebSocket(
        [],
        deferred_message_batches=[
            _response_batch("resp_ws_prewarm", output_tokens=0),
            _response_batch("resp_ws_turn"),
        ],
    )
    _install_upstreams(monkeypatch, [upstream])
    prewarm = _prewarm_frame()
    permissions = _message("developer", "<permissions instructions>sandbox</permissions instructions>")
    agents_md = _message("developer", "# AGENTS.md instructions")
    user = _message("user", "fix the bug")
    turn_start = {
        "type": "response.create",
        "model": "gpt-5.6-sol",
        "instructions": "",
        "previous_response_id": "resp_ws_prewarm",
        "input": [permissions, _message("system", "system note"), agents_md, user],
        "stream": True,
    }

    with TestClient(app_instance) as client:
        with client.websocket_connect(
            "/backend-api/codex/responses", headers=_connection_headers("thread-ws-developer-prewarm")
        ) as websocket:
            for request in (prewarm, turn_start):
                websocket.send_text(json.dumps(request))
                events = [json.loads(websocket.receive_text()) for _ in range(2)]
                assert [event["type"] for event in events] == ["response.created", "response.completed"]

    sent_payloads = [json.loads(text) for text in upstream.sent_text]
    assert len(sent_payloads) == 2
    assert sent_payloads[0]["input"] == prewarm["input"]
    chained = sent_payloads[1]
    assert chained["previous_response_id"] == "resp_ws_prewarm"
    # Developer messages keep their place in input; only the system message,
    # which upstream rejects in input, moves into instructions.
    assert chained["input"] == [permissions, agents_md, user]
    assert chained["instructions"] == "system note"


_CODEX_METADATA = "internal_chat_message_metadata_passthrough"


def _developer_with(**extra: object) -> dict[str, object]:
    return {**_message("developer", "<permissions instructions>sandbox</permissions instructions>"), **extra}


def _receive_terminal(websocket) -> dict[str, object]:
    while True:
        event = json.loads(websocket.receive_text())
        if event.get("type") in {"response.completed", "response.failed", "error"}:
            return event


def _terminal_error_code(terminal: dict[str, object]) -> object:
    response = terminal.get("response")
    error = response.get("error") if isinstance(response, dict) else terminal.get("error")
    return error.get("code") if isinstance(error, dict) else None


def _run_anchor_rejection(
    app_instance,
    monkeypatch: pytest.MonkeyPatch,
    *,
    turn: dict[str, object],
    prewarm: dict[str, object] | None,
    path: str = "/backend-api/codex/responses",
    resends: tuple[dict[str, object], ...] = (),
) -> tuple[dict[str, object], _SequencedUpstreamWebSocket, list[int]]:
    """Send ``turn`` after an optional prewarm; upstream rejects the turn's anchor.

    Returns the turn's terminal event. ``resends`` are the frames the client
    sends next on the same socket, as a client told to drop the anchor does;
    each must complete, the first as ``resp_ws_resent_1`` and so on.
    """
    first_upstream = _SequencedUpstreamWebSocket(
        [],
        deferred_message_batches=[
            *([_response_batch("resp_ws_anchor", output_tokens=0)] if prewarm is not None else []),
            _previous_response_not_found("resp_ws_anchor"),
        ],
    )
    replay_upstream = _SequencedUpstreamWebSocket(
        [],
        deferred_message_batches=[
            *(_response_batch(f"resp_ws_resent_{index}") for index in range(1, len(resends) + 1)),
            # A replay the proxy must not make still completes, so the
            # assertions see it.
            _response_batch("resp_ws_replayed"),
        ],
    )
    connects = _install_upstreams(monkeypatch, [first_upstream, replay_upstream])
    headers = _connection_headers("thread-ws-developer-replay")

    with TestClient(app_instance) as client:
        with client.websocket_connect(path, headers=headers) as websocket:
            if prewarm is not None:
                websocket.send_text(json.dumps(prewarm))
                assert [json.loads(websocket.receive_text())["type"] for _ in range(2)] == [
                    "response.created",
                    "response.completed",
                ]
            websocket.send_text(json.dumps(turn))
            terminal = _receive_terminal(websocket)
            for frame in resends:
                websocket.send_text(json.dumps(frame))
                assert _receive_terminal(websocket)["type"] == "response.completed"
    return terminal, replay_upstream, connects


def _turn(input_items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "type": "response.create",
        "model": "gpt-5.6-sol",
        "instructions": "",
        "previous_response_id": "resp_ws_anchor",
        "input": input_items,
        "stream": True,
    }


@pytest.mark.parametrize(
    ("prewarm", "turn_input"),
    [
        pytest.param(
            None,
            [_message("developer", "new permissions"), _message("user", "continue")],
            id="unknown-anchor-developer-and-user",
        ),
        pytest.param(
            None,
            [
                _developer_with(**{_CODEX_METADATA: {"content_item_kinds": ["model.developer_instructions"]}}),
                _message("user", "continue"),
            ],
            id="unknown-anchor-developer-with-content-kinds",
        ),
        pytest.param(
            None,
            [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "perm"}, {"type": "input_text", "text": ""}],
                },
                _message("user", "continue"),
            ],
            id="unknown-anchor-developer-with-blank-part",
        ),
        pytest.param(
            None,
            [_developer_with(status="in_progress", name="x"), _message("user", "continue")],
            id="unknown-anchor-developer-with-extra-keys",
        ),
        pytest.param(
            _prewarm_frame(),
            [
                _message("developer", "<permissions instructions>sandbox</permissions instructions>"),
                _message("developer", "# AGENTS.md instructions"),
                _message("user", "fix the bug"),
            ],
            id="prewarm-anchor-developer-messages-and-user",
        ),
    ],
)
def test_rejected_turn_delta_is_not_replayed_without_its_anchor(app_instance, monkeypatch, prewarm, turn_input):
    """A turn delta that lost its anchor fails closed instead of replaying as a new conversation.

    Without the anchor the replay would drop everything the anchor carried:
    the prewarm's tool bundle and base instructions, or the earlier turns.
    """
    terminal, replay_upstream, connects = _run_anchor_rejection(
        app_instance,
        monkeypatch,
        turn=_turn(turn_input),
        prewarm=prewarm,
    )

    assert terminal["type"] != "response.completed"
    assert replay_upstream.sent_text == []
    assert connects[0] == 1


def test_v1_messages_alias_delta_with_empty_developer_is_not_replayed(app_instance, monkeypatch):
    """The alias coerces ``content: ""`` into a blank text part; it is still no history."""
    turn = _turn([])
    del turn["input"]
    turn["messages"] = [{"role": "developer", "content": ""}, {"role": "user", "content": "continue"}]

    terminal, replay_upstream, connects = _run_anchor_rejection(
        app_instance, monkeypatch, turn=turn, prewarm=None, path="/v1/responses"
    )

    assert terminal["type"] != "response.completed"
    assert replay_upstream.sent_text == []
    assert connects[0] == 1


def test_standard_codex_first_turn_after_empty_prewarm_is_resent_without_its_lost_anchor(app_instance, monkeypatch):
    """A captured standard Codex first turn chained to an empty prewarm loses its anchor.

    A client-supplied anchor is never replayed without it: the client is told
    the response is gone and resends the turn without it. The resent turn keeps
    its developer message in ``input``, without its client item id, as the
    hoist dropped it.
    """
    body = json.loads(Path("tests/fixtures/codex_bodies/captured_gpt55_standard_http.json").read_text())
    prewarm = {**body, "type": "response.create", "generate": False, "input": []}
    turn = {**body, "type": "response.create", "previous_response_id": "resp_ws_anchor"}

    terminal, replay_upstream, connects = _run_anchor_rejection(
        app_instance, monkeypatch, turn=turn, prewarm=prewarm, resends=({**body, "type": "response.create"},)
    )

    assert terminal["type"] == "response.failed"
    assert _terminal_error_code(terminal) == "previous_response_not_found"
    assert connects[0] == 2
    expected_input = [
        {key: value for key, value in item.items() if not (item.get("role") == "developer" and key == "id")}
        for item in body["input"]
    ]
    [resent] = [json.loads(text) for text in replay_upstream.sent_text]
    assert resent.get("previous_response_id") is None
    assert resent["input"] == expected_input


def test_anchored_tool_continuation_trims_echoed_output_around_a_developer_update(app_instance, monkeypatch):
    """A developer update between the echoed prior output and the tool result stays; the echo goes."""
    upstream = _SequencedUpstreamWebSocket([], deferred_message_batches=[_response_batch("resp_ws_tool_output")])
    _install_upstreams(monkeypatch, [upstream])
    developer_update = _message("developer", "new permissions")
    tool_output = {"type": "function_call_output", "call_id": "call_repeat", "output": "Wed May 6 16:00:00 UTC 2026"}
    request_payload = {
        "type": "response.create",
        "model": "gpt-5.4",
        "instructions": "",
        "previous_response_id": "resp_prev_tool_call",
        "input": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "running command"}]},
            {"type": "function_call", "call_id": "call_repeat", "name": "exec_command", "arguments": '{"cmd":"date"}'},
            developer_update,
            tool_output,
        ],
        "stream": True,
    }

    with TestClient(app_instance) as client:
        with client.websocket_connect("/backend-api/codex/responses") as websocket:
            websocket.send_text(json.dumps(request_payload))
            assert [json.loads(websocket.receive_text())["type"] for _ in range(2)] == [
                "response.created",
                "response.completed",
            ]

    sent_payload = json.loads(upstream.sent_text[0])
    assert sent_payload["previous_response_id"] == "resp_prev_tool_call"
    assert sent_payload["input"] == [developer_update, tool_output]


@pytest.mark.parametrize(
    "first_turn_input",
    [
        pytest.param([_message("system", "Return JSON"), _message("user", "list the planets")], id="json-mode"),
        pytest.param(
            [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}],
                },
                _message("user", "what is shown?"),
            ],
            id="developer-image",
        ),
        pytest.param(
            [
                {
                    "role": "system",
                    "content": [
                        {"type": "reasoning", "text": "Use JSON"},
                        {"type": "input_text", "text": "Use metric measurements."},
                    ],
                },
                _message("user", "List the planets in JSON"),
            ],
            id="json-mention-only-in-a-sanitized-part",
        ),
    ],
)
def test_first_turn_resent_after_a_lost_anchor_anchors_the_next_turn(app_instance, monkeypatch, first_turn_input):
    """Inputs the old hoist kept (a JSON-mode instruction, non-text parts) stay history.

    A chained first turn after an empty prewarm loses its anchor and is not
    replayed without it; the client resends it without the anchor. The resent
    turn's stored context is the one its raw request had, so the client's next
    full history anchors to the resent response and sends only the new turn.
    """
    turn = _turn(first_turn_input)
    if first_turn_input[0]["role"] == "system":
        turn["text"] = {"format": {"type": "json_object"}}
    resend = {key: value for key, value in turn.items() if key != "previous_response_id"}
    next_user = _message("user", "Explain the previous answer in JSON.")
    follow_up = {**resend, "input": [*first_turn_input, next_user]}

    terminal, replay_upstream, connects = _run_anchor_rejection(
        app_instance,
        monkeypatch,
        turn=turn,
        prewarm=dict(_prewarm_frame(), input=[]),
        resends=(resend, follow_up),
    )

    assert terminal["type"] == "response.failed"
    assert _terminal_error_code(terminal) == "previous_response_not_found"
    assert connects[0] == 2
    resent, anchored = [json.loads(text) for text in replay_upstream.sent_text]
    assert resent.get("previous_response_id") is None
    assert anchored["previous_response_id"] == "resp_ws_resent_1"
    assert anchored["input"] == [next_user]


def test_full_resend_after_developer_only_turn_is_not_anchored(app_instance, monkeypatch):
    """A turn of developer messages alone leaves no stored history to anchor on, as when they were hoisted."""
    upstream = _SequencedUpstreamWebSocket(
        [], deferred_message_batches=[_response_batch("resp_ws_first"), _response_batch("resp_ws_second")]
    )
    _install_upstreams(monkeypatch, [upstream])
    developer = _message("developer", "Design a table")
    answer = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Here is a table"}]}
    frames = [
        {"type": "response.create", "model": "gpt-5.6-sol", "instructions": "", "input": [developer], "stream": True},
        {
            "type": "response.create",
            "model": "gpt-5.6-sol",
            "instructions": "",
            "input": [developer, answer, _message("user", "Continue")],
            "stream": True,
        },
    ]

    with TestClient(app_instance) as client:
        with client.websocket_connect(
            "/backend-api/codex/responses",
            headers={"session_id": "thread-ws-developer-only-turn", "openai-beta": "responses_websockets=2026-02-06"},
        ) as websocket:
            for frame in frames:
                websocket.send_text(json.dumps(frame))
                assert [json.loads(websocket.receive_text())["type"] for _ in range(2)] == [
                    "response.created",
                    "response.completed",
                ]

    second = json.loads(upstream.sent_text[1])
    assert "previous_response_id" not in second
    assert second["input"] == frames[1]["input"]


def test_trimmed_lite_resend_keeps_developer_boundaries_for_tool_call_dedupe(app_instance, monkeypatch):
    """A suffix trimmed from a Lite resend keeps its developer messages as dedupe boundaries."""
    upstream = _SequencedUpstreamWebSocket(
        [], deferred_message_batches=[_response_batch("resp_ws_first"), _response_batch("resp_ws_second")]
    )
    _install_upstreams(monkeypatch, [upstream])
    prefix = [
        {
            "type": "additional_tools",
            "role": "developer",
            "tools": [{"type": "function", "name": "write_stdin", "parameters": {"type": "object"}}],
        },
        _message("developer", "base instructions"),
        _message("user", "poll"),
    ]
    call = {"type": "function_call", "name": "write_stdin", "arguments": '{"session_id":7,"chars":""}', "call_id": "c1"}
    output = {"type": "function_call_output", "call_id": "c1", "output": "waiting"}
    frames = [
        {"type": "response.create", "model": "gpt-5.6-sol", "instructions": "", "input": prefix, "stream": True},
        {
            "type": "response.create",
            "model": "gpt-5.6-sol",
            "instructions": "",
            "input": [
                *prefix,
                call,
                output,
                _message("developer", "Poll again"),
                {**call, "call_id": "c2"},
                {**output, "call_id": "c2", "output": "done"},
                _message("user", "summarize"),
            ],
            "stream": True,
        },
    ]

    with TestClient(app_instance) as client:
        with client.websocket_connect(
            "/backend-api/codex/responses",
            headers={"session_id": "thread-ws-lite-dedupe", "openai-beta": "responses_websockets=2026-02-06"},
        ) as websocket:
            for frame in frames:
                websocket.send_text(json.dumps(frame))
                assert [json.loads(websocket.receive_text())["type"] for _ in range(2)] == [
                    "response.created",
                    "response.completed",
                ]

    second = json.loads(upstream.sent_text[1])
    assert second["previous_response_id"] == "resp_ws_first"
    assert [item["call_id"] for item in second["input"] if item.get("type") == "function_call"] == ["c1", "c2"]


_WS_QUESTION = _message("user", "first question")
_WS_ANSWER = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}
_WS_FOLLOW_UP = _message("user", "second question")
_WS_CHANGED = _message("developer", "sandbox=danger-full-access")
_WS_FRENCH = _message("developer", "Use French.")
_WS_ENGLISH = _message("developer", "Use English.")


def _ws_mode(mode: str) -> dict[str, object]:
    return _message("developer", f"<collaboration_mode># {mode} Mode</collaboration_mode>")


def _ws_baseline(skills: str) -> dict[str, object]:
    """A captured-style two-part developer message: the Default mode and the skills list."""
    item = _ws_mode("Default")
    return {**item, "content": [*cast(list[object], item["content"]), {"type": "input_text", "text": skills}]}


_WS_BASELINE_OLD = _ws_baseline("Skills: testing")
_WS_BASELINE_NEW = _ws_baseline("Skills: testing, linting")
_WS_PLAN = _ws_mode("Plan")
_WS_THIRD = _message("user", "third question")


@pytest.mark.parametrize(
    ("first_input", "second_input", "expected_second_input"),
    [
        pytest.param(
            [_message("developer", "sandbox=read-only"), _WS_QUESTION],
            [_WS_CHANGED, _WS_QUESTION, _WS_ANSWER, _WS_FOLLOW_UP],
            [_WS_CHANGED, _WS_ANSWER, _WS_FOLLOW_UP],
            id="changed-developer",
        ),
        # One stored occurrence covers one occurrence: the repeated French
        # instruction reverses the stored English one and must still go.
        pytest.param(
            [_WS_FRENCH, _WS_ENGLISH, _WS_QUESTION],
            [_WS_FRENCH, _WS_ENGLISH, _WS_QUESTION, _WS_FRENCH, _WS_ANSWER, _WS_FOLLOW_UP],
            [_WS_FRENCH, _WS_ANSWER, _WS_FOLLOW_UP],
            id="repeated-instruction-reverses-a-later-one",
        ),
        # A refreshed earlier developer message is followed again by the later
        # ones of the range, so Plan stays the last mode.
        pytest.param(
            [_WS_BASELINE_OLD, _WS_QUESTION, _WS_ANSWER, _WS_PLAN, _WS_FOLLOW_UP],
            [_WS_BASELINE_NEW, _WS_QUESTION, _WS_ANSWER, _WS_PLAN, _WS_FOLLOW_UP, _WS_ANSWER, _WS_THIRD],
            [_WS_BASELINE_NEW, _WS_PLAN, _WS_ANSWER, _WS_THIRD],
            id="skills-refresh",
        ),
    ],
)
def test_warm_socket_anchor_forwards_a_changed_developer_message(
    app_instance, monkeypatch, first_input, second_input, expected_second_input
):
    """The session anchor carries what the stored turn sent; a changed developer message still goes."""
    upstream = _SequencedUpstreamWebSocket(
        [], deferred_message_batches=[_response_batch("resp_ws_first"), _response_batch("resp_ws_second")]
    )
    _install_upstreams(monkeypatch, [upstream])
    frames = [
        {"type": "response.create", "model": "gpt-5.6-sol", "instructions": "", "input": turn_input, "stream": True}
        for turn_input in (first_input, second_input)
    ]

    with TestClient(app_instance) as client:
        with client.websocket_connect(
            "/backend-api/codex/responses",
            headers={"session_id": "thread-ws-changed-developer", "openai-beta": "responses_websockets=2026-02-06"},
        ) as websocket:
            for frame in frames:
                websocket.send_text(json.dumps(frame))
                assert [json.loads(websocket.receive_text())["type"] for _ in range(2)] == [
                    "response.created",
                    "response.completed",
                ]

    second = json.loads(upstream.sent_text[1])
    assert second["previous_response_id"] == "resp_ws_first"
    assert second["input"] == expected_second_input


_WS_MODE_PATTERN = re.compile(r"<collaboration_mode># (\w+) Mode</collaboration_mode>")


def _ws_last_mode(items: Sequence[Mapping[str, object]]) -> str | None:
    modes = [
        mode
        for item in items
        if item.get("role") == "developer"
        for mode in _WS_MODE_PATTERN.findall(json.dumps(item.get("content")))
    ]
    return modes[-1] if modes else None


def test_warm_socket_keeps_the_clients_last_collaboration_mode(app_instance, monkeypatch):
    """Default, then Plan, a skills refresh, then Default again: the chain upstream ends with the client's mode."""
    history = [_WS_QUESTION, _WS_ANSWER, _WS_PLAN, _WS_FOLLOW_UP]
    default_again = [_WS_ANSWER, _ws_mode("Default"), _message("user", "fourth question")]
    turns: list[list[Mapping[str, object]]] = [
        [_WS_BASELINE_OLD, _WS_QUESTION],
        [_WS_BASELINE_OLD, *history],
        [_WS_BASELINE_NEW, *history, _WS_ANSWER, _WS_THIRD],
        [_WS_BASELINE_NEW, *history, _WS_ANSWER, _WS_THIRD, *default_again],
        [_WS_BASELINE_NEW, *history, _WS_ANSWER, _WS_THIRD, *default_again, _WS_ANSWER, _message("user", "fifth")],
    ]
    upstream = _SequencedUpstreamWebSocket(
        [], deferred_message_batches=[_response_batch(f"resp_ws_mode_{index}") for index in range(len(turns))]
    )
    _install_upstreams(monkeypatch, [upstream])

    with TestClient(app_instance) as client:
        with client.websocket_connect(
            "/backend-api/codex/responses",
            headers={"session_id": "thread-ws-mode-round-trip", "openai-beta": "responses_websockets=2026-02-06"},
        ) as websocket:
            for turn_input in turns:
                websocket.send_text(
                    json.dumps(
                        {
                            "type": "response.create",
                            "model": "gpt-5.6-sol",
                            "instructions": "",
                            "input": turn_input,
                            "stream": True,
                        }
                    )
                )
                assert [json.loads(websocket.receive_text())["type"] for _ in range(2)] == [
                    "response.created",
                    "response.completed",
                ]

    frames = [json.loads(text) for text in upstream.sent_text]
    assert [frame.get("previous_response_id") for frame in frames] == [
        None,
        *(f"resp_ws_mode_{index}" for index in range(len(turns) - 1)),
    ]
    chain: list[dict[str, object]] = []
    for frame, client_input in zip(frames, turns, strict=True):
        chain.extend(frame["input"])
        assert _ws_last_mode(chain) == _ws_last_mode(client_input)
