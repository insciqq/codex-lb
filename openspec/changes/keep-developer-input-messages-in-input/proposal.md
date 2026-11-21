# Keep Developer Input Messages in `input`

## Why

Responses normalization hoists every `system`/`developer`-role input message into the top-level `instructions` field.
The upstream Responses API carries a previous response's `input` forward through `previous_response_id`, but not its
`instructions`. Developer content hoisted on one request is therefore missing from every response chained after it,
without an error (issue #2563).

Codex CLI hits this on every WebSocket session that starts with a prewarm. The prewarm holds `additional_tools`, so the
Responses Lite rule leaves it alone. The first turn then sends `previous_response_id` plus the session's developer
messages (permissions, AGENTS.md, skills) and no `additional_tools`, so they are hoisted. Every later request in the
session (tool outputs) sends empty `instructions`, so from the second model call on the model no longer sees those
messages. The hoist also reorders the prompt, so the first turn misses the prompt cache.

The upstream rejects `system`-role input messages (`400 System messages are not allowed`), but accepts `developer`-role
input messages with or without top-level `instructions`, so only `system` messages need hoisting.

## What Changes

- Responses and compact normalization hoist only `system`-role instruction messages into `instructions`.
- `developer`-role instruction messages stay in `input`, unchanged and in their original position.
- A request without top-level `instructions` whose `input` holds a developer message still defaults `instructions` to
  `""`, so it validates and forwards.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `responses-api-compat`: "Non-message system and developer input items are preserved" hoists only `system` messages.

## Impact

- `app/core/openai/requests.py` (`_normalize_responses_input_instructions`).
- Chat Completions default mapping (`chat-completions-compat`) is unchanged: it merges `system`/`developer` messages into
  `instructions` itself. In `json_object` mode, which keeps those messages in `input`, a `developer` message now reaches
  the upstream in `input` as that requirement states, instead of being hoisted afterwards; `system` messages are still
  hoisted.
- Requests with `additional_tools` are unchanged (Responses Lite rule).
- Replay heuristics that decided delta versus full resend, or proved retained output, on input without developer
  messages now judge two explicit views of `input` (classifier and proof), so replay behaviour stays as it was. See
  the ADDED requirement.
- One prompt-cache miss per existing thread after the change, replay classification outside Responses Lite that
  ignores inline developer messages, a compact budget that now counts developer text, and `system` messages that
  are still hoisted: see `context.md`.
