## Why

`deliver-http-bridge-denied-anchor-refusal-to-native-clients` introduced the
local pre-dispatch refusal provenance and recorded it on one site, and
`deliver-retry-circuit-suppression-to-native-clients` added the two
retry-circuit suppressions. The HTTP bridge raises many more refusals of its
own before a turn is enqueued or sent upstream, and they reuse the transport
codes `stream_incomplete`, `upstream_request_timeout` and
`upstream_unavailable`: a closed or retiring session, a stream lease lost
without a settings snapshot, unavailable owner, ring or durable metadata, a
lost continuation, an incompatible admission handoff, a shutdown while a turn
waits for a session and account-neutral recovery sessions whose durable fence
is unavailable. The native Codex lifecycle took each of them for a dead
upstream, so the client received a committed `200` with an empty body instead
of the error (upstream issue 2388).

Whether a refusal precedes its own attempt is not enough: one client turn can
run on several request states, and a recovery or owner-forward retry can be
refused after an earlier frame of the turn went upstream and may still be
running there.

## What Changes

- The denied-anchor fence and the retry-circuit suppressions keep their
  existing unconditional provenance.
- Every other refusal the bridge raises before the attempt it refuses is sent
  is built as a candidate by one constructor (a refused durable claim is marked
  at the session-creation call site). The candidate itself is not a local
  pre-dispatch refusal.
- A turn-wide marker shared by every request state of the client turn records
  whether a generating frame of the turn was sent: it is set at every
  `response.create` send and when a failed owner forward may have dispatched
  the turn, and it is never cleared. `_stream_via_http_bridge`, which every
  bridge refusal of the turn passes through, raises a turn-owned copy of a
  candidate marked as a local pre-dispatch refusal only while the marker is
  unset, so turns sharing one candidate never affect each other.
- Refusals that are not provably pre-dispatch keep the transport lifecycle:
  a failed prewarm (observed on the upstream leg), a candidate raised after a
  frame of the turn was sent (even one upstream rejected), a turn with upstream
  response events, a send failure, refusals raised inside the session reconnect
  (shared with replays after dispatch), replay failures of dispatched requests,
  reader and close failures of enqueued requests, and shared account-selection,
  refresh and connect errors outside the bridge.
- Public status, code and message are unchanged; non-native clients see no
  difference.

## Impact

`app/modules/proxy/_service/http_bridge/` (request submission, session
resolution helpers, session registry, owner forwarding, request entry), the
request state in `app/modules/proxy/_service/support.py`, and the HTTP bridge
tests. No setting, schema, affinity, account-selection or prompt-cache change.

This change modifies the requirement text written by
`deliver-http-bridge-denied-anchor-refusal-to-native-clients` and must be
archived after it.
