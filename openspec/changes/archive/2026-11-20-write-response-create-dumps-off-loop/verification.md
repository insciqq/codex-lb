Regression reproduced: with the dump writer held, rejecting an oversized
`response.create` blocked the caller on the event loop for the whole write.
Shutdown moved past the proxy drains while an admitted dump was still writing.

Validation after the fix:
- `tests/unit/test_response_create_dump_off_loop.py`,
  `tests/integration/test_response_create_dump_shutdown.py`, the oversized
  `response.create` tests in `tests/integration/test_http_responses_bridge.py`
  and `tests/integration/test_proxy_websocket_responses.py`, and the proxy
  timing-seam check passed.
- Ruff lint and formatting checks passed.
- Full `ty check` passed.
- Strict change validation passed.
