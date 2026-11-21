# Context

## Upstream wire shapes

Probed against `chatgpt.com/backend-api/codex/responses` (HTTP, `gpt-6.1-sol`):

| `input` | top-level `instructions` | Result |
|---|---|---|
| developer message + user message | non-empty | 200 |
| developer message + user message | `""` | 200 |
| developer message + user message | absent | 200 |
| user message only | absent | 200 |
| system message + user message | non-empty | 400 `{"detail":"System messages are not allowed"}` |
| developer or user message with `reasoning_content`, `reasoning_details`, `tool_calls`, or `function_call` | `""` | 400 `unknown_parameter` |
| developer message with a `reasoning` content part | `""` | 400 invalid content part type |

Hoisting was introduced to avoid that rejection (#950); it only needs to apply to `system` messages. The
interleaved-reasoning sanitizer still applies to developer messages: upstream rejects those keys on every message role,
so forwarding them would turn a silently cleaned request into a 400.

## Chained responses

`previous_response_id` carries the previous response's `input` and output items forward, but not its `instructions`.
Each request's `instructions` apply to that request only. Moving a developer message from `input` to `instructions`
therefore changes what every chained response sees, not just the request being normalized.

Example (issue #2563 reproduction, about 12.8k tokens of reference data in one developer message, then a chained
question about it):

- Direct: `input_tokens` 12830, answers from the reference data.
- Hoisting proxy: `input_tokens` 34, "I don't have reference data".
- This change: `input_tokens` 12830, answers from the reference data.

## Consequences

- Prompt prefix: a thread that existed before this change sees its developer text move from `instructions` back
  into `input` once, so its next request misses the prompt cache once. The derived prompt-cache anchor (its domain
  includes `instructions`) and the inferred HTTP bridge key are re-derived once for the same reason. No compatibility
  switch is kept.
- Replay and continuity: every decision judges the base-equivalent input, the list the old hoist produced (see the
  `responses-api-compat` requirement "Replay and continuity decisions judge the base-equivalent input"), so replay
  behaviour, stored contexts and tool-call dedupe match the behaviour before this change, and stored contexts
  recorded before it stay valid. An anchored request that trims a stored prefix still sends a new or changed
  developer message from that range (before, the current developer text reached every request through
  `instructions`) and skips one the stored turn already sent. After a reattach from a durable row the stored turn's
  developer messages are unknown, so that one request sends the developer messages of the trimmed range again.
- Operation keys: durable operation and recovery-attempt records from before the change still match an identical
  retry, through a second lookup of the key with the developer text in `instructions`.
- Account neutrality: the gate judges item shapes on the base-equivalent input and still rejects account-owned
  references anywhere in the forwarded input. A developer message the hoist would have moved whole is forwarded
  without its client item id, as the hoist dropped it.
- Compact budget: developer messages now count toward the compact input budget and are trim anchors, so a compact
  request with a single very large developer message can now fail with `responses_compact_input_too_large`, where the
  same text in `instructions` was not counted.
- Developer messages without content are dropped, as the hoist used to drop them.
- Limitation: `system` messages are still hoisted, because upstream rejects them in `input`. A `system` message on a
  request chained through `previous_response_id` therefore applies to that request only.
