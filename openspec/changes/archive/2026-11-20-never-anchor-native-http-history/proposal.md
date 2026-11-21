## Why

Observed in production: native Codex clients on a thread-bound HTTP bridge
received `502 stream_incomplete` ("The previous response anchor was rejected
upstream; retry the request.") after upstream closed the WebSocket that
completed the thread's last response. Upstream resolves a `store=false`
`previous_response_id` only on the socket that produced it. The bridge still
put the stored response on the new socket as a proxy anchor and trimmed the
history. Upstream answered `previous_response_not_found`, the local rebind
retried the same denied anchor, and the fence refused it. The client's own
retry repeated the sequence or hit the ambiguous-operation fence, although
the request already carried the complete history.

Review of a socket-bound variant (anchor only on the producing socket) found
that tracking the producing socket leaked into retries, recovery and
authentication handling, and still anchored histories to a response the
client had not continued from.

## What Changes

- A native Codex HTTP request without a client `previous_response_id` or
  `conversation` and with a non-empty input list is treated as complete
  history.
- The proxy never adds its own `previous_response_id` to such a request, on a
  live socket or after a socket loss. The client's body goes upstream
  unchanged on the same account with the same prompt cache key; upstream
  prompt caching still matches its prefix.
- The request keeps what its anchored form had: a transport-only replay stays
  on the thread's owner, an open hard-key retry circuit admits it like a
  proof-gated full resend, and a failed bridge connect for an owner-bound
  thread is not replayed over raw HTTP. A recovery checkpoint never fences it.
- The older full-resend requirement that keeps a durable anchor is scoped to
  requests that are not native history.
- Account selection, owner routing, retries after explicit errors and account
  health handling are unchanged. Client-supplied anchors and other clients
  keep the existing behavior; their anchored requests are byte-for-byte as
  before.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `responses-api-compat`: the proxy never anchors native Codex HTTP history.

## Impact

HTTP bridge anchor planning. No settings, migrations, dependencies or UI
changes.
