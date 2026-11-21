# Tasks

## 1. Implementation

- [x] 1.1 `_normalize_responses_input_instructions` forwards `developer`-role messages unchanged and still defaults
  missing `instructions` to `""` for them; only `system`-role messages are hoisted.

- [x] 1.2 Developer messages with no content left after part sanitization are dropped, on `input` and on both
  `messages` aliases.
- [x] 1.3 One base-equivalent input view (`base_equivalent_input` in `app/core/openai/requests.py`, the old hoist rule
  reused) drives every replay and continuity decision: the bridge full-resend classifier, stored-context
  recording and matching, stored-prefix and prior-output trims (mapped back to wire positions), the durable and compact
  proofs, cross-transport replay eligibility, the unanchored replay after a pre-visible quota rejection, and replayed
  tool-call dedupe. The account-neutrality gate judges item shapes on
  the same view and rejects account-owned references anywhere.
- [x] 1.4 The stored context records the fingerprints of the stored turn's developer messages; the bridge
  store-context and recovery trims and the WebSocket session-anchor trim send new or changed developer messages from
  the trimmed range and skip ones already sent (all of them when the set is unknown).
- [x] 1.5 The old hoist's Lite classification and JSON-mode decisions are recorded on the raw request
  (`BaseHoist`), so trimmed copies and sanitized parts are judged as before; operation and recovery-attempt lookups
  also try the pre-change frame key; developer item ids the hoist dropped are not forwarded.

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
  a developer-image first turn after an empty prewarm that lose their anchor are not replayed, and the client's resend
  keeps its developer messages and anchors the next turn; a full resend after a developer-only turn is not
  anchored; echo trim around a developer update), `tests/unit/test_proxy_http_bridge_developer_messages.py`
  (cold-bridge anchor for developer deltas of any shape; preserved non-Lite full resends including content-kinds,
  blank-part and developer-only prefixes; a developer-only previous turn leaves no stored context; a prepended developer
  message keeps the warm-session anchor; echo trim around a developer update; dedupe across a developer update; no
  cross-transport failover on a repeated developer instruction), `tests/unit/test_replay_safety.py` (gate on inline
  developer messages), `tests/unit/test_openai_requests.py` (the base-equivalent view), the compact owner-loss and
  developer-id failover tests, the `messages` alias route tests in `tests/integration/test_openai_compat_features.py`
  and `tests/integration/test_proxy_compact.py`, and the pre-visible quota replay in
  `tests/integration/test_proxy_transient_retry.py` (a developer prefix moves, a developer-only prefix stays).
- [x] 2.5 `tests/integration/test_http_bridge_developer_messages.py` (seven insert/change/trailing/removed cases on a
  warm bridge across three turns; developer attachment still promotes history), the WebSocket changed-developer and
  Lite-dedupe tests, the sanitized-JSON resend after a lost anchor, the pre-upgrade operation retry, the developer-id
  failover, the compact trailing-developer terminal case and the goal marker on a later developer line.
- [x] 2.6 Occurrence-by-occurrence developer matching (HTTP bridge and WebSocket), a changed text beside an unchanged
  attachment, a developer record used only for its own response (branch from an older response, after a
  developer-only turn), pre-upgrade operation keys against frames captured from the old normalizer (developer before
  system, blank developer), a JSON-kept developer id that keeps failover on the owner, the next turn after a WebSocket
  resend, compact consumed-pair cleanup behind a trailing developer message, and the goal marker beside an attachment.
- [x] 2.7 A skills refresh of an earlier developer message keeps the later Plan-mode message, and a Default, Plan,
  Default round trip keeps the client's last collaboration mode upstream (HTTP bridge and WebSocket).
- [x] 2.8 Position-by-position developer matching: reordered Default and Plan messages are sent in client order, a
  JSON-mode skills refresh also sends the later Default message the hoist kept inline, the round trip sends exactly the
  expected frames, an unknown record resends in client order and leaves Lite history alone, and anchored turns of the
  captured Codex bodies send no developer message again (HTTP bridge and WebSocket).
- [x] 2.9 A developer message sent again carries no client item id (a JSON-kept message after a skills refresh, a
  message with an attachment after a refresh, changed text beside an attachment), and the turn after sends none again
  (HTTP bridge and WebSocket).
