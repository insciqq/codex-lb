from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.openai.requests import ResponsesCompactRequest, ResponsesRequest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "item",
    [
        {"type": ["tool_search_call"], "call_id": "c"},
        {"type": {"nested": "message"}, "role": "user", "content": "hi"},
        {"role": ["user"], "content": "hi"},
    ],
)
@pytest.mark.parametrize("model", [ResponsesRequest, ResponsesCompactRequest])
def test_non_string_item_discriminators_are_rejected_at_validation(model, item) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({"model": "gpt-5.4", "instructions": "", "input": [item]})


def test_string_discriminators_still_validate() -> None:
    request = ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                {"type": "tool_search_call", "call_id": "c", "arguments": {"query": "x"}},
            ],
        }
    )
    assert isinstance(request.input, list)
