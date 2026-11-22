## MODIFIED Requirements

### Requirement: Native Codex preserves upstream failure lifecycle

For a native Codex HTTP/SSE Responses request, an upstream transport timeout or
stream EOF without a terminal Responses event MUST terminate the downstream
stream without synthesizing `response.failed`, `error`, or `[DONE]`. The proxy
MUST still execute reservation, request-log, account-health, and owned-resource
cleanup before propagating the termination. Non-native and OpenAI-compatible
clients MUST retain the existing stable terminal-error shaping.

This rule applies only to failures the proxy observed on the upstream leg. A
failure the proxy manufactured itself before any upstream frame was sent for the
request, and which it records as a local pre-dispatch refusal, MUST NOT be
delivered as a silent transport termination to a native Codex client. When the
downstream response has not committed, such a refusal MUST be returned as its
HTTP status with the error envelope in the body. When the downstream response
has already committed, it MUST be delivered as a terminal `response.failed`
event carrying the same error code, followed by `[DONE]`, and that terminal MUST
NOT be marked as a synthetic transport failure. A native Codex client MUST NOT
receive a committed `200` with zero bytes and no terminal event for a recorded
local pre-dispatch refusal.

The HTTP bridge MUST record that provenance on its denied-anchor
before-dispatch fence and on its retry-circuit suppressions. For every other
refusal it raises itself before the attempt it refuses is enqueued or sent
upstream, including one that reuses the `stream_incomplete`,
`stream_idle_timeout`, `upstream_request_timeout` or `upstream_unavailable`
code, it MUST record the provenance only while no generating frame of the
client turn has been sent upstream by any request state that serves the turn,
and while no internal bridge owner forward of the turn may have dispatched it
without the owner identifying its error as a local pre-dispatch refusal. A
frame of the turn counts as sent from its send attempt on, even when upstream
later rejects it. That class covers the refusals raised while the bridge
resolves, reuses or creates the session for the turn, such as unavailable
owner, ring or durable metadata, a lost continuation, an incompatible admission
handoff, a shutdown while the turn waits for a session, or an account-neutral
recovery session whose durable fence is unavailable, and the refusals the
submission raises above its pending-request enqueue and upstream send, such as
a closed or retiring session or a stream lease lost without a settings
snapshot. The proxy MUST NOT record the provenance on a failure it observed on
the upstream leg, including a failed prewarm, on a refusal of a turn that holds
upstream response events, or on a refusal raised by a path that replays already
dispatched requests, such as the session reconnect. When several turns receive
the same refusal, the verdict for one turn MUST NOT change the error delivered
to another. The proxy MUST carry the provenance across an internal bridge owner
forward so the origin instance reaches the same verdict as the owner.

#### Scenario: Native Codex sees a truncated SSE lifecycle

- **GIVEN** a native Codex HTTP request has received a non-terminal SSE event
- **WHEN** upstream closes without a terminal event
- **THEN** downstream closes without a synthetic terminal event or `[DONE]`
- **AND** proxy cleanup and failure accounting still complete

#### Scenario: Non-native client keeps the terminal umbrella

- **GIVEN** an OpenAI SDK or other non-native client receives the same upstream
  truncation
- **WHEN** codex-lb normalizes the stream
- **THEN** the client receives the existing terminal `response.failed` shape

#### Scenario: Native Codex sees a local pre-dispatch refusal before commit

- **GIVEN** a native Codex HTTP Responses request
- **WHEN** the proxy refuses it before sending any upstream frame and the
  downstream response has not committed
- **THEN** the client receives the refusal's HTTP status and error envelope
- **AND** the body is not an empty event stream

#### Scenario: Native Codex sees a local pre-dispatch refusal after commit

- **GIVEN** a native Codex HTTP Responses request
- **WHEN** the proxy refuses it before sending any upstream frame and the
  downstream response has already committed
- **THEN** the client receives a terminal `response.failed` with the same error
  code
- **AND** `[DONE]`
- **AND** the terminal is not marked as a synthetic transport failure

#### Scenario: Native Codex sees a session-resolution refusal

- **GIVEN** a native Codex HTTP bridge turn
- **WHEN** the bridge refuses it while resolving its session because owner
  metadata is unavailable or the continuation is lost
- **THEN** the client receives `502` with `upstream_unavailable` or
  `stream_incomplete` and the refusal message
- **AND** no upstream frame is sent for the turn

#### Scenario: Native Codex sees a submission refusal on a closed session

- **GIVEN** a native Codex HTTP bridge turn whose session closed or started
  retiring before the turn was enqueued
- **WHEN** the submission refuses the turn
- **THEN** the client receives `502` with `upstream_unavailable` and the
  refusal message
- **AND** no upstream frame is sent for the turn

#### Scenario: A refusal after an unanswered frame keeps the transport lifecycle

- **GIVEN** a native Codex HTTP bridge turn whose frame was sent and not
  answered by upstream
- **WHEN** the bridge rejects the turn itself and then refuses its recovery
  while resolving a session
- **THEN** the refusal is not recorded as a local pre-dispatch refusal
- **AND** the client receives a committed `200` with no bytes

#### Scenario: A refusal after a rejected frame keeps the transport lifecycle

- **GIVEN** a native Codex HTTP bridge turn whose frame upstream rejected with
  `previous_response_not_found`
- **WHEN** the bridge refuses the recovery retry because its session closed
  before the retry was sent
- **THEN** the refusal is not recorded as a local pre-dispatch refusal

#### Scenario: Waiters sharing one refusal receive their own verdicts

- **GIVEN** two HTTP bridge turns waiting on one session creation, one of which
  already sent a generating frame
- **WHEN** the creation fails with one refusal
- **THEN** only the turn that sent nothing receives it as a local pre-dispatch
  refusal

#### Scenario: A failed prewarm keeps the transport lifecycle

- **GIVEN** a native Codex HTTP bridge turn whose session warm-up frame upstream
  answered with a failure
- **WHEN** the bridge fails the turn before sending its own frame
- **THEN** the failure is not recorded as a local pre-dispatch refusal

#### Scenario: An owner replica marks a session-resolution refusal

- **GIVEN** a native Codex HTTP bridge turn forwarded to its owner replica
- **WHEN** the owner refuses it while resolving the session
- **THEN** the owner's error response identifies the failure as a local
  pre-dispatch refusal

#### Scenario: An unmarked owner rejection leaves the turn possibly dispatched

- **GIVEN** a native Codex HTTP bridge turn whose owner replica answered the
  forward with an error it did not identify as a local pre-dispatch refusal
- **WHEN** the origin recovers locally and refuses the turn itself
- **THEN** the origin's refusal is not recorded as a local pre-dispatch refusal
