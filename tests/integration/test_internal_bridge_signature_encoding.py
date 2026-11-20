from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.config.settings import get_settings
from app.core.openai.requests import ResponsesRequest
from app.modules.proxy.http_bridge_forwarding import (
    HTTP_BRIDGE_SIGNATURE_HEADER,
    HTTP_BRIDGE_SIGNATURE_V2_HEADER,
    HTTPBridgeForwardContext,
    build_owner_forward_headers,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_internal_bridge_rejects_non_ascii_signature_with_400(async_client: AsyncClient) -> None:
    payload = ResponsesRequest.model_validate({"model": "gpt-5.4", "instructions": "", "input": "hi", "stream": True})
    context = HTTPBridgeForwardContext(
        origin_instance="origin-a",
        target_instance=get_settings().http_responses_session_bridge_instance_id,
        codex_session_affinity=True,
        downstream_turn_state="turn_a",
    )
    signed = build_owner_forward_headers(headers={}, payload=payload, context=context)
    # Raw bytes reach the ASGI app as sent; Starlette decodes them as latin-1.
    garbled = {HTTP_BRIDGE_SIGNATURE_HEADER, HTTP_BRIDGE_SIGNATURE_V2_HEADER}
    headers = [
        (name.encode("ascii"), (b"\xe9" + value[1:].encode("ascii")) if name in garbled else value.encode("latin-1"))
        for name, value in signed.items()
    ]

    response = await async_client.post(
        "/internal/bridge/responses",
        headers=headers,
        json=payload.model_dump_for_forwarding(),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bridge_forward_invalid"
