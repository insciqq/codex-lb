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
- Replay heuristics: outside Responses Lite, they judge `input` as they did while developer messages were hoisted
  (see the `responses-api-compat` requirement "Replay heuristics judge history without inline developer messages").
  The delta-or-full-resend classifiers ignore every developer message, because they fail safe by calling an input a
  delta; the retained-output proofs ignore only account-neutral ones, because they fail safe by rejecting. Lite
  input keeps its own developer rules. Replay behaviour is otherwise unchanged.
- Compact budget: developer messages now count toward the compact input budget and are trim anchors, so a compact
  request with a single very large developer message can now fail with `responses_compact_input_too_large`, where the
  same text in `instructions` was not counted.
- Developer messages without content are dropped, as the hoist used to drop them.
- Limitation: `system` messages are still hoisted, because upstream rejects them in `input`. A `system` message on a
  request chained through `previous_response_id` therefore applies to that request only.
