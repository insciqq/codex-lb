from __future__ import annotations

from dataclasses import replace

import pytest

from app.core.openai.requests import ResponsesRequest
from app.db.models import StickySessionKind
from app.modules.proxy._service.http_bridge.helpers import _make_http_bridge_session_key
from app.modules.proxy.affinity import _AffinityPolicy
from app.modules.proxy.http_continuation import http_continuation_signal, inferred_http_bridge_key

pytestmark = pytest.mark.unit


def _payload(items, **kwargs):
    return ResponsesRequest(model="gpt-5.4", instructions="Instructions", input=items, **kwargs)


@pytest.mark.parametrize(
    ("items", "signal"),
    [
        ([{"role": "user", "content": "hi"}], None),
        ([{"role": "user", "content": "hi"}, {"role": "user", "content": "more"}], None),
        ([{"role": "system", "content": "policy"}, {"role": "user", "content": "hi"}], None),
        ([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}], None),
        (
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "more"},
            ],
            "history",
        ),
        ([{"type": "function_call_output", "call_id": "call_a", "output": "done"}], "tool_result"),
        ([{"type": "custom_tool_call_output", "call_id": "call_a", "output": "done"}], "tool_result"),
    ],
)
def test_continuation_evidence(items, signal):
    assert http_continuation_signal(_payload(items), {}) == signal


def test_tools_declared_alone_do_not_promote():
    payload = _payload("hi", tools=[{"type": "function", "name": "read", "parameters": {}}])
    assert http_continuation_signal(payload, {}) is None


def test_history_key_uses_complete_initial_input_and_is_soft_and_scoped():
    first = "a" * 512
    items = [
        {"role": "user", "content": first + "one"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "next"},
    ]
    payload = _payload(items)
    key = inferred_http_bridge_key(payload)
    extended = _payload([*items, {"role": "assistant", "content": "again"}, {"role": "user", "content": "next"}])
    assert inferred_http_bridge_key(extended) == key
    distinct = _payload([{**items[0], "content": first + "two"}, *items[1:]])
    assert inferred_http_bridge_key(distinct) != key
    assert inferred_http_bridge_key(_payload(items, conversation="conv_a")) != key
    affinity = _AffinityPolicy(key="truncated-cache", kind=StickySessionKind.PROMPT_CACHE)
    bridge_key = _make_http_bridge_session_key(payload, headers={}, affinity=affinity, api_key=None, request_id="req")
    assert bridge_key.affinity_key == key
    assert bridge_key.strength == "soft"
    assert replace(bridge_key, api_key_id="key_a") != replace(bridge_key, api_key_id="key_b")


def test_history_key_is_the_key_upstream_main_computed():
    """Developer messages stay in ``input``; the key is computed on upstream main's hoisted view.

    Upstream main moved every developer message into ``instructions``, so the
    key equals the key of the same turn in that shape, and two threads that
    differ only in a developer message keep distinct locality.
    """

    def history(*developer_texts: str) -> list[dict[str, str]]:
        later = [{"role": "developer", "content": text} for text in developer_texts[1:]]
        return [
            {"role": "developer", "content": developer_texts[0]},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            *later,
            {"role": "user", "content": "more"},
        ]

    def upstream_shape(items: list[dict[str, str]]) -> ResponsesRequest:
        texts = [item["content"] for item in items if item["role"] == "developer"]
        rest = [item for item in items if item["role"] != "developer"]
        return ResponsesRequest(model="gpt-5.4", instructions="\n".join(["Instructions", *texts]), input=rest)

    affinity = _AffinityPolicy(key="derived-cache", kind=StickySessionKind.PROMPT_CACHE)

    def bridge_key(payload: ResponsesRequest) -> str:
        return _make_http_bridge_session_key(
            payload, headers={}, affinity=affinity, api_key=None, request_id="req"
        ).affinity_key

    for texts in (("Answer as a pirate.",), ("Answer as a pirate.", "Be brief.")):
        items = history(*texts)
        assert bridge_key(_payload(items)) == bridge_key(upstream_shape(items))
    assert bridge_key(_payload(history("Answer as a poet."))) != bridge_key(_payload(history("Answer as a pirate.")))


def test_json_mode_follow_up_with_an_image_instruction_is_history():
    """Upstream hoisted the JSON text and left the image as a ``user`` item after the assistant turn."""
    payload = ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "Instructions",
            "input": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {
                    "role": "developer",
                    "content": [
                        {"type": "input_text", "text": "Reply in JSON about this image."},
                        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                    ],
                },
            ],
            "text": {"format": {"type": "json_object"}},
        }
    )
    assert http_continuation_signal(payload, {}) == "history"
    key = inferred_http_bridge_key(payload)
    assert key is not None and key.startswith("http-history:")
