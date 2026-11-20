## Why

JSON mode keeps a `system` or `developer` message that mentions JSON in
Responses `input`, because upstream only counts JSON mentions inside input
messages. `/v1/responses` also accepts a Chat-style `messages` alias, and its
message coercion hoisted every instruction message into `instructions` before
that rule ran, so JSON-mode requests sent through `messages` still lost their
only JSON mention.

## What Changes

- Keep instruction roles in `input` while coercing `/v1/responses` `messages`
  for JSON-mode requests, so the existing Responses normalization decides
  which instruction stays in `input`.

## Impact

- Affected capability: `responses-api-compat`.
- Affected code: `app/core/openai/v1_requests.py`.
- Requests without JSON mode and compact requests are unchanged.
