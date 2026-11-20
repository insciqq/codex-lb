## Why

JSON mode keeps a `system` message that mentions JSON in Responses `input`,
because upstream only counts JSON mentions inside input messages, and
`developer` messages stay in `input` because `previous_response_id` does not
carry `instructions` into chained responses. `/v1/responses` and
`/v1/responses/compact` also accept a Chat-style `messages` alias, and its
message coercion hoisted every instruction message into `instructions` before
those rules ran, so JSON-mode requests sent through `messages` lost their only
JSON mention and developer messages sent through `messages` were dropped from
chained responses.

## What Changes

- Keep instruction roles in `input` while coercing `/v1/responses` and
  `/v1/responses/compact` `messages`, so the Responses normalization decides
  which instruction moves into `instructions`, exactly as for `input`.

## Impact

- Affected capability: `responses-api-compat`.
- Affected code: `app/core/openai/v1_requests.py`.
- `system` messages sent through `messages` are hoisted as before.
