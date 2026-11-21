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
  `instructions`) and skips one the stored turn already sent. The stored turn's developer messages are recorded for that exact
  response: a request anchored to another response, a turn without a stored prefix, or a reattach from a durable row
  that differs from the in-memory record leaves them unknown, so that one request sends the developer messages of the
  trimmed range again. Each recorded occurrence covers one occurrence on the next request, and once one developer message of the range is
  sent every later one in it is sent too, so the last instruction upstream is the client's last one; a developer message whose
  attachment the hoist kept as a `user` item is matched as a whole message.
- Operation keys: durable operation and recovery-attempt records from before the change still match an identical
  retry, through a second lookup of the key rebuilt in the old frame shape (the `instructions` text the old hoist
  produced from the raw request, recorded at validation).
- Account neutrality: the gate judges item shapes on the base-equivalent input and still rejects account-owned
  references anywhere in the forwarded input. A developer message the hoist would have moved whole is forwarded
  without its client item id, as the hoist dropped it; any item id left on the frame the replacement account would
  receive keeps the request on its owner.
- Compact budget: developer messages now count toward the compact input budget and are trim anchors, so a compact
  request with a single very large developer message can now fail with `responses_compact_input_too_large`, where the
  same text in `instructions` was not counted.
- Developer messages without content are dropped, as the hoist used to drop them.
- Rolling deploy: a replica of this release cannot forward a request with an inline developer message to an owner
  replica of an earlier release (the owner hoists it before verifying the forwarding signature, `400
  bridge_forward_invalid`). A multi-replica deployment must stop its old replicas before the new ones serve;
  single-replica deployments are unaffected.
- Transitional: a recovery checkpoint recorded before the change in the `REPLAYED` state is keyed with the developer
  text in `instructions`; an identical retry after the upgrade records a new checkpoint under the new key and can
  dispatch that consumed turn once more. Rollback to the previous release is not symmetric for operation keys either:
  records written in the new frame shape are not found under the old key, so an identical retry of a consumed turn
  can dispatch once more.
- Limitation: `system` messages are still hoisted, because upstream rejects them in `input`. A `system` message on a
  request chained through `previous_response_id` therefore applies to that request only.
