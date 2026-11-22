# Context

## Criterion

The denied-anchor fence (`deliver-http-bridge-denied-anchor-refusal-to-native-clients`)
and the two retry-circuit suppressions
(`deliver-retry-circuit-suppression-to-native-clients`) keep their earlier,
unconditional provenance; this change does not touch them.

Every other bridge refusal is a local pre-dispatch refusal only when two things
hold:

- it is raised before the attempt it refuses is enqueued or sent, and
- no generating frame of the client turn has been sent upstream.

Recording one that does not meet both could hide a real transport failure from
the native Codex client, which is worse than the empty `200` this change
removes. Upstream may still be running an unanswered frame of the turn, and
the bridge itself manufactures previous-response rejections after an unanswered
send, so even a rejected frame is not treated as proof.

The first condition is positional: these sites build their error as a
candidate with `_http_bridge_local_refusal`, or mark it at the call site. The
second is the turn-wide `_HTTPBridgeTurnDispatch` marker:

- `prepare_bridge_request`, the only builder of the turn's request states
  (initial, local recovery, owner-forward recovery, account-neutral replay),
  attaches the marker to every state;
- `_send_http_bridge_request_text_with_archive_id` sets it at every
  `response.create` attempt, whatever the send's outcome;
- a failed owner forward sets it unless the forward never reached the owner or
  the owner identified its error as a local pre-dispatch refusal;
- nothing clears it.

`_stream_via_http_bridge`, which every bridge refusal of the turn passes
through, raises a turn-owned copy of a candidate marked as a local pre-dispatch
refusal while the marker is unset, and the unmarked candidate otherwise. The
candidate itself is never changed: one instance can reach several turns, for
example every waiter of one session creation.

The per-attempt `response_create_attempt_count` keeps its retry and lease
roles only. A fresh recovery state starts at zero, so it cannot prove that the
turn sent nothing.

## Per-site verdicts

The four codes are `stream_incomplete`, `stream_idle_timeout`,
`upstream_request_timeout` and `upstream_unavailable`. "Flagged" means built
as a candidate through `_http_bridge_local_refusal` (or marked at the call
site) and delivered as a local pre-dispatch refusal only while the turn marker
is unset; "unconditional" means the earlier static flag.

| Site | Code | Verdict | Evidence |
|---|---|---|---|
| submission retry-circuit gate | upstream_request_timeout 503 | unconditional (unchanged) | submission entry, before reconnect and send |
| verified stale-anchor replay generation claim | upstream_request_timeout 503 | unconditional (unchanged) | directly above the enqueue |
| denied-anchor fence | stream_incomplete 502 | unconditional (unchanged, upstream) | directly above the enqueue; the fence fires after upstream itself denied the anchor |
| submission: session retiring | upstream_unavailable 502 | flagged | before admission, enqueue and send |
| submission: closed session unregistered | upstream_unavailable 502 | flagged | before admission; no reconnect attempted |
| submission: closed session not recovered | upstream_unavailable 502 | flagged | the reconnect does not send the turn (`send_request=False`) |
| submission: closed or replaced after admission | upstream_unavailable 502 | flagged | under the lifecycle lock, before the enqueue |
| `_ensure_http_bridge_session_stream_lease_locked`: lease lost without snapshot | upstream_unavailable 503 | flagged | only called by the submission before its enqueue |
| same: session closed during lease reacquire | upstream_unavailable 502 | flagged | same |
| request entry: durable continuity lookup failed | upstream_unavailable 502 | flagged | before session resolution |
| session resolution: owner instance lookup failed | upstream_unavailable 502 | flagged | session resolution precedes the attempt's submission |
| session resolution: active ring lookup failed | upstream_unavailable 502 | flagged | same |
| session resolution: continuity lost (stale account-neutral alias, incompatible alias session, incompatible previous-response session, continuation without a session) | stream_incomplete 502 | flagged | same |
| `_http_bridge_parallel_fork_key`: incompatible session for a continuation | stream_incomplete 502 | flagged | only called by session resolution |
| `_raise_http_bridge_incompatible_admission_handoff` | upstream_unavailable 503 | flagged | only called by session resolution |
| `close_all_http_bridge_sessions`: bridge shutting down | upstream_unavailable 503 | flagged | delivered only to session-resolution waiters on in-flight session creations |
| `_fail_closed_http_bridge_recovery_lease` | upstream_unavailable 502 | flagged | only reached from the session-reuse lease renewal |
| session creation: durable claim refused (account-neutral claim without durable tables) | upstream_unavailable 502 | flagged at the creation call site | the shared `_claim_durable_http_bridge_session` also serves `_claim_http_bridge_replacement_before_swap` for reconnect and security-work replay, where its refusal stays unflagged |
| `_maybe_prewarm_http_bridge_session`: prewarm failed | upstream_unavailable 502 | not flagged | observed on the upstream leg: the warm-up frame was sent and the reader may report its socket's death |
| submission: turn already has upstream response events | upstream_unavailable 502 | not flagged | the turn was dispatched |
| submission: send failure | stream_incomplete or transport code 502 | not flagged | the frame may have reached upstream |
| `_reconnect_http_bridge_session`: reader did not shut down, bound account excluded | upstream_unavailable 502 | not flagged | shared with replays of dispatched requests; on the submission path their errors surface as the flagged closed-session refusal |
| `_http_bridge_precreated_retry_failure_error` | upstream_unavailable 502 | not flagged | replay of a dispatched request |
| reader, terminal, idle-timeout, local-reset, close and stale-pending failures | stream_incomplete, stream_idle_timeout, upstream_request_timeout, upstream_unavailable | not flagged | they fail enqueued or dispatched requests |
| rewritten upstream `previous_response_not_found` | stream_incomplete | not flagged | upstream answered the dispatched turn |
| shared account selection, budget exhaustion, token refresh and websocket connect errors during session creation | upstream_request_timeout, upstream_unavailable | not flagged | outside the bridge and shared with reconnects after dispatch; selection budget exhaustion is a local refusal that still reaches a native client as an empty `200` |
| eventless-timeout cooldown refusals | bridge_eventless_timeout | not affected | not one of the four codes |

A flagged refusal raised after a frame of the turn was sent reaches the native
client on the transport lifecycle. That includes a recovery retry refused after
upstream definitively rejected the first frame. It is conservative: the empty
`200` it produced before this change remains for that case.

## Examples

A native Codex client sends a continuation while the durable bridge store is
unreachable. The request entry fails closed with `502 upstream_unavailable`
"HTTP bridge owner metadata unavailable; retry later." Before this change the
client received `200` and an empty body; now it receives the `502` envelope,
or, if the response already committed, one `response.failed` with that code
followed by `[DONE]`.

A continuation reattaches to a dead owner, its frame is sent and upstream stays
silent. The post-submit cooldown check rejects the anchor itself and the
recovery cannot resolve a session. A frame of the turn was sent, so the same
owner-metadata refusal ends the stream without a terminal event.
