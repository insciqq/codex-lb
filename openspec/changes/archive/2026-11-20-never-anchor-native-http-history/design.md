## Context

Codex sends its complete local history on every HTTP Responses request and
never supplies `previous_response_id` there. The HTTP bridge forwards those
requests over an upstream WebSocket and used to add its own anchor to send
only the new items. Upstream keeps a `store=false` response only for the
socket that produced it, so after a clean close or a service restart the
stored response is gone even though the bridge's durable row still names it.

## Goals / Non-Goals

Stop sending an anchor that may not resolve, for requests whose body is
already complete, with the smallest change. Keep the account choice, owner
routing and the cache key. Do not change behavior for clients that may send
only a delta and rely on the proxy's anchor, or for client-supplied anchors.

## Decisions

- Identify complete history by protocol, not by inference: a native Codex
  client (the route's decision: a native `User-Agent` or `originator` and no
  explicit OpenAI SDK marker), no client `previous_response_id`, no
  `conversation`, and a non-empty input list. Fingerprints of the stored
  prefix and the tool-call manifest cannot show that a body equals the
  anchored context, so they are not used.
- Never anchor such a request. The anchor is only an optimization of request
  size; upstream prompt caching matches the unchanged prefix on the same
  account either way. Anchoring only with proof would need the producing
  socket, the response the client continued from and its pending calls, and
  a socket-bound variant showed that tracking this leaks into replay, retry
  and authentication paths.
- Reuse the existing durable-anchor suppression flag that a quarantine sets
  for full-resend payloads; native history sets it for every request. Every
  injection point already honors it: fresh reattach, hydration of the
  durable anchor, session-level injection and owner-forward recovery.
- Leave routing alone. The verified-full-resend owner decision runs before the
  injection is skipped, so a stale broad session owner is still ignored and
  the request keeps the durable owner as its preferred account.
- Keep the protections the anchored form carried. The request state records
  that it is native history. The retry circuit admits it as a proof-gated
  replay, a transport-only replay never treats it as a fresh hard request
  (an id-free history would otherwise look portable and leave the owner), and
  a pre-dispatch session failure for an owner-bound thread keeps the marker
  that stops the raw HTTP replay, which cannot require the owner.
- Skip the recovery-checkpoint fence for native history. It fences replays of
  an ambiguous anchored operation; native history is the client's own
  complete request with client-side tools, and a checkpoint written by an
  older anchored attempt never settles once the session is gone.
- Log the per-request "Large response.create prepared" size warning at DEBUG
  for native history, which crosses the warning size on every turn of a long
  thread; the size limit itself is unchanged.

## Risks / Trade-offs

- Every native HTTP request carries the full history upstream. The cost is
  request size, not cache affinity: the account and prompt cache key are
  unchanged.
- A client that sends native identity headers but only a delta over HTTP would
  have its body forwarded as written. Codex does not do this.
- A socket that drops with no events is an ordinary transport failure of an
  unanchored request: the bridge's existing replay sends the same body again.

## Migration Plan

No data migration or setting. Reverting the code restores the previous
anchoring.
