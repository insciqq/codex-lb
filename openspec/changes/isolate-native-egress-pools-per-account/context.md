# Per-account native egress connection pools

## Failure mode

reqwest pools connections per client and per origin. The helper used one
client per connector policy, so all accounts shared one HTTP/2 connection to
the upstream host. A connection-level failure (peer reset, GOAWAY, TLS record
error) fails every stream on that connection, which surfaced as simultaneous
transport errors on unrelated accounts. HTTP/2 keepalive pings were not
adopted instead: a late PING acknowledgement makes the client close the
connection, which on a shared connection again fails every account's
streams.

## Key

`native_egress_pool_key(account_id)` returns the first 32 hex characters of
`sha256("codex-lb-native-egress-pool\0" + account_id)` for the load-balancer
account id. The helper treats the value as opaque, never logs it, and only
uses it as part of a map key. The upstream account id is not a fallback:
several load-balancer accounts (seats of one workspace) share it, and keying
by it would put their streams back on one connection. Every per-account
caller (proxy streams and compact, warmups, automations, the quota planner,
the account probe) passes its load-balancer account; a caller that names none
uses the shared pool, the previous behavior.

Control-plane requests (usage, model discovery, files, transcription) send no
key and keep the shared client; they are short and do not hold long streams.

## Lifetime

A pool entry is in use while a request task holds its lease. reqwest closes
a client's idle connections after 120 seconds. Building a client loads the
native root store on the helper's command loop, so an unused client is kept
for 30 minutes before it is evicted on a later lookup; an unconnected client
is cheap. At most 256 unused clients are kept, the one being leased included,
least recently used evicted first. Entries in use are never evicted, so the
cache holds at most 256 clients plus those of the requests in flight, and a
deleted or idle account leaves it within 30 minutes of its last request.
Sharing one prebuilt TLS configuration through `use_preconfigured_tls` was
not chosen: reqwest uses such a configuration as given, so its ALPN and TLS
settings would have to be reproduced by hand to keep the measured Codex wire
profile, for a cost the longer client lifetime already removes.

The compact path subtracts the time already spent from its connect timeout,
and the connect timeout is part of the client key. It is rounded down to a
100 ms step, so compact requests of one account share a client; rounding down
never exceeds the remaining budget, and a remainder below one step is floored
to whole milliseconds.
Whole seconds would have halved the default two-second connect timeout.

## Older helpers

An older helper completes the handshake, since the field is optional, and
silently ignores `pool_key`; all accounts then share connections again. That
happens when the Python app is updated without rebuilding the helper, for
example when only `app/` is synced into a development container. The new
helper advertises `http_pool_key_v1`, and the adapter logs one warning per
helper process when the capability is missing instead of failing the
handshake, so the mismatch is visible without breaking an older helper.

## Timeouts on the wire

The adapter floors request and connect timeouts to whole milliseconds, so a
budget of 0.0996 s becomes 99 ms rather than 100 ms. The compact step uses
the same conversion, so the stepped value is exactly what the helper
receives. A budget below the 1 ms protocol minimum gets that minimum, the
same clamp `_remaining_total_timeout` applies to an exhausted budget.

## Example

Accounts A and B stream concurrently. A's HTTP/2 connection receives a
GOAWAY: A's in-flight streams fail through the existing error path, while
B's streams continue on B's connection. A's next request opens a new
connection in A's pool.
