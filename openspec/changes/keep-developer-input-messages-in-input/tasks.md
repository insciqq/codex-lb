# Tasks

## 1. Implementation

- [x] 1.1 `_normalize_responses_input_instructions` forwards `developer`-role messages unchanged and still defaults
  missing `instructions` to `""` for them; only `system`-role messages are hoisted.

- [x] 1.2 Developer messages with no content left after part sanitization are dropped, on `input` and on both
  `messages` aliases.
- [x] 1.3 One base-equivalent input view (`base_equivalent_input` in `app/core/openai/requests.py`, the old hoist rule
  reused) drives every replay and continuity decision: the WebSocket and bridge full-resend classifiers, stored-context
  recording and matching, stored-prefix and prior-output trims (mapped back to wire positions), the durable and compact
  proofs, cross-transport replay eligibility, and replayed tool-call dedupe. The account-neutrality gate accepts Codex
  developer metadata outside Lite; compact strips developer item ids from replacement payloads.

## 2. Verification

- [x] 2.1 Unit tests `test_responses_input_developer_message_stays_in_input` (Responses and compact, with and without
  `instructions`) and `test_responses_input_developer_message_is_sanitized_like_other_messages`; updated `test_responses_input_system_message_moves_to_instructions`,
  `test_responses_input_non_message_system_and_developer_items_are_preserved` and
  `test_responses_input_developer_message_keeps_single_non_text_part`, the integration test
  `test_backend_responses_preserves_non_message_developer_directive` and the chat JSON-mode test
  `test_chat_response_format_json_object_keeps_json_instruction_in_input`. The `chat_responses_shaped` and
  `v1_compact_input` cases of `tests/fixtures/passthrough_request_corpus.json` keep their developer message in `input`.
- [x] 2.2 Live check through a patched 1.24.0 container with the issue #2563 reproduction: both scenarios answer from
  the developer message (about 12.8k input tokens, prompt cache hit), matching a direct upstream connection.
- [x] 2.3 ruff, pytest, strict OpenSpec validation.
- [x] 2.4 Product-path regressions: `tests/integration/test_proxy_websocket_developer_messages.py` (prewarm-chained
  turn keeps developer messages; rejected deltas with developer messages of any shape, and the `/v1/responses` alias
  with an empty developer message, are not replayed; a captured standard Codex first turn, a JSON-mode first turn and
  a developer-image first turn after an empty prewarm still recover; a full resend after a developer-only turn is not
  anchored; echo trim around a developer update), `tests/unit/test_proxy_http_bridge_developer_messages.py`
  (cold-bridge anchor for developer deltas of any shape; preserved non-Lite full resends including content-kinds,
  blank-part and developer-only prefixes; a developer-only previous turn leaves no stored context; a prepended developer
  message keeps the warm-session anchor; echo trim around a developer update; dedupe across a developer update; no
  cross-transport failover on a repeated developer instruction), `tests/unit/test_replay_safety.py` (gate on inline
  developer messages), `tests/unit/test_openai_requests.py` (the base-equivalent view), the compact owner-loss and
  developer-id failover tests, and the `messages` alias route tests in `tests/integration/test_openai_compat_features.py`
  and `tests/integration/test_proxy_compact.py`.
