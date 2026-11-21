## Why

The HTTP bridge retry circuit refuses a turn with a 503
`upstream_request_timeout` before any upstream frame for it is sent: at the
submission gate while a hard key cools down or holds its half-open lease, and
at the dispatch claim of a verified stale-anchor replay whose circuit
generation advanced. Neither refusal was recorded as a local pre-dispatch
refusal, so the native Codex failure lifecycle treated it as a dead upstream:
before commit it replayed the refusal into the committed body, and after commit
it ended the body without a terminal event. Native clients saw a `200` with an
empty body instead of the cooldown error and its retry hint.

## What Changes

- Both retry-circuit suppression refusals carry the local pre-dispatch refusal
  provenance that `deliver-http-bridge-denied-anchor-refusal-to-native-clients`
  introduced for the denied-anchor fence. Native clients receive the 503 and
  its `Retry-After` before commit, and an unmarked terminal `response.failed`
  with the SSE retry hint after commit.
- The origin of an internal bridge forward keeps the owner's `Retry-After` on
  the rebuilt error, so the hint reaches its client across the owner hop.
- Non-native clients are unaffected: the synthetic transport marker that the
  flag suppresses is stripped before a non-native client sees the stream.

## Impact

`app/modules/proxy/_service/http_bridge/request_submit.py` and the HTTP bridge
integration tests. No setting, schema, affinity or account-selection change.
