# Tasks

## 1. Implementation

- [x] 1.1 `_normalize_responses_input_instructions` forwards `developer`-role messages unchanged and still defaults
  missing `instructions` to `""` for them; only `system`-role messages are hoisted.

- [x] 1.2 Developer messages with no content left after part sanitization are dropped, on `input` and on both
  `messages` aliases.
- [x] 1.3 Replay heuristics judge history through two views (`replay_safety`): the classifier view omits every
  developer message (WebSocket retry classifier, HTTP bridge full-resend classifier, and the bridge's retry-safety
  decision after a stored-prefix trim); the proof view omits only account-neutral developer messages (durable
  retained-output and tool-manifest proofs, compact replay proof). A verified developer-only stored prefix still
  counts; compact keeps a developer-only prefix owner-bound. Prior-output trims keep interleaved developer messages in
  order. Replay behaviour otherwise matches the behaviour before this change.

## 2. Verification

- [x] 2.1 Unit tests `test_responses_input_developer_message_stays_in_input` (Responses and compact, with and without
  `instructions`) and `test_responses_input_developer_message_is_sanitized_like_other_messages`; updated `test_responses_input_system_message_moves_to_instructions`,
  `test_responses_input_non_message_system_and_developer_items_are_preserved` and
  `test_responses_input_developer_message_keeps_single_non_text_part`, the integration test
  `test_backend_responses_preserves_non_message_developer_directive` and the chat JSON-mode test
  `test_chat_response_format_json_object_preserves_instruction_roles_in_input`. The `chat_responses_shaped` and
  `v1_compact_input` cases of `tests/fixtures/passthrough_request_corpus.json` keep their developer message in `input`.
- [x] 2.2 Live check through a patched 1.24.0 container with the issue #2563 reproduction: both scenarios answer from
  the developer message (about 12.8k input tokens, prompt cache hit), matching a direct upstream connection.
- [x] 2.3 ruff, pytest, strict OpenSpec validation.
- [x] 2.4 Product-path regressions: `tests/integration/test_proxy_websocket_developer_messages.py` (prewarm-chained
  turn keeps developer messages; rejected deltas with developer messages of any shape, and the `/v1/responses` alias
  with an empty developer message, are not replayed; a captured standard Codex first turn after an empty prewarm still
  recovers; echo trim around a developer update), `tests/unit/test_proxy_http_bridge_developer_messages.py`
  (cold-bridge anchor for developer deltas of any shape, preserved non-Lite full resends including a developer-only
  or content-kinds prefix, a trimmed developer-only prefix stays non-retryable, echo trim around a developer update),
  `tests/unit/test_replay_safety.py` (non-Lite proof view, developer-only prefix; the Lite developer-rule tests now use
  Lite input), the compact owner-loss tests, and the `messages` alias route tests in
  `tests/integration/test_openai_compat_features.py` and `tests/integration/test_proxy_compact.py`.
