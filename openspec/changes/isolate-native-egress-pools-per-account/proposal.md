## Why

The native egress helper caches one reqwest client per connector policy
(proxy URL, connect timeout, response decoding), with no account dimension.
Every account's Responses streams to the same upstream host are therefore
multiplexed onto one HTTP/2 connection, and a single connection reset, GOAWAY,
or TLS failure fails the in-flight streams of every account at once. The
cached clients are also never evicted, and compact requests shave the
milliseconds already spent off their connect timeout, so nearly every compact
request adds a new client, connection, and root-store load for the life of
the helper.

## What Changes

- Python sends an opaque per-account `pool_key` with native Responses
  streaming, compact, and account-probe HTTP requests, direct and routed.
  The key is a truncated SHA-256 digest of the load-balancer account id; no
  identifier, token, or email crosses IPC. The upstream account id is never
  used as a key, because several load-balancer accounts can share it. Every
  per-account caller, including warmups, automations, and the quota planner,
  names its load-balancer account.
- The helper adds the key to its client cache key, so each account gets its
  own client and its own connections. Requests without a key keep the shared
  client, so an older adapter behaves as before.
- The helper advertises `http_pool_key_v1`. The adapter does not require it,
  so an older helper still works, but logs one warning per helper process
  when it is missing, because such a helper ignores the key and shares
  connections across accounts.
- Native HTTP request and connect timeouts are floored to whole
  milliseconds, so the helper never gets more time than the remaining
  budget; a budget below the 1 ms protocol minimum gets that minimum.
- The helper evicts a cached client that no request has used for 30 minutes,
  well past the 120-second connection idle timeout, and keeps at most 256
  unused clients, counting the one being leased, evicting the least recently
  used. A client with a request in flight is never evicted.
- The compact connect timeout is rounded down to a 100 ms step, so compact
  requests reuse one client per account. Whole seconds were not used: the
  default two-second connect timeout would have become one second.
- WebSocket transport, account selection, routing, and prompt-cache
  affinity are unchanged.

## Impact

Transport failures stay within the account whose connection failed. Each
active account opens its own upstream connection, so an account idle for
longer than the connection idle timeout pays a new TCP and TLS handshake on
its next request, as it would have with a shared connection that had gone
idle. A compact connect timeout can be up to 100 ms shorter than before.
The wire change is one optional request field and one advertised, optional
capability; there is no new setting, dependency, schema change, or dashboard
surface.
