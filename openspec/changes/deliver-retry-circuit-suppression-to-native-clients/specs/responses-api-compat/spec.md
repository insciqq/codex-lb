## ADDED Requirements

### Requirement: Retry-circuit suppression is a local pre-dispatch refusal

When the HTTP bridge retry circuit refuses a turn before any upstream frame for
it was sent, the proxy MUST record the refusal as a local pre-dispatch refusal.
This covers the submission-gate suppression during a hard-key cooldown or
half-open lease and the suppression of a verified stale-anchor replay whose
circuit generation advanced before its dispatch claim. A native Codex client
MUST receive such a refusal as its `503` error envelope with `Retry-After` when
the downstream response has not committed, and as an unmarked terminal
`response.failed` carrying `upstream_request_timeout` and the SSE retry hint
followed by `[DONE]` when it has. It MUST NOT receive a committed `200` with an
empty body. When the refusal comes from the owner replica of an internal bridge
forward, the origin MUST keep the owner's `Retry-After` value so its client
receives the same hint.

#### Scenario: Native client refused by the retry circuit before commit

- **GIVEN** a native Codex HTTP bridge turn whose hard key is cooling down
- **WHEN** the retry circuit refuses the submission before the response commits
- **THEN** the client receives `503` with `upstream_request_timeout` and `Retry-After`
- **AND** no upstream frame is sent for the turn

#### Scenario: Native client refused by the retry circuit after commit

- **GIVEN** a native Codex HTTP bridge turn whose response has already committed
- **WHEN** the retry circuit refuses the submission
- **THEN** the stream ends with a `response.failed` carrying `upstream_request_timeout`
- **AND** the event carries the retry hint and is not marked as a synthetic transport failure
- **AND** the stream ends with `[DONE]`

#### Scenario: Owner replica refusal keeps its cooldown hint at the origin

- **GIVEN** a native Codex HTTP bridge turn forwarded to its owner replica
- **WHEN** the owner answers with the retry-circuit `503` and `Retry-After: 30`
- **THEN** the origin's client receives `Retry-After: 30` before commit
- **AND** the SSE retry hint of 30000 ms after commit
