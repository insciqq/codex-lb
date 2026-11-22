## Implementation

- [x] Add the optional `pool_key` field to the native request contract.
- [x] Key helper HTTP clients by `pool_key` and evict unused clients by idle
  time and by count, never while a request holds the client.
- [x] Send the digest key from native Responses streaming, compact, and
  account-probe requests, direct and routed through `CodexClient`, and name
  the load-balancer account at every per-account caller.
- [x] Round the compact connect timeout down to a 100 ms step and floor
  native timeouts to whole milliseconds.
- [x] Advertise `http_pool_key_v1` and warn once per helper process when a
  helper lacks it, without requiring it.
- [x] Add Python tests for the key derivation and plumbing, workspace seats,
  failover, and the connect timeout; Rust unit tests for partitioning and
  eviction; and helper-boundary tests that keyed requests use separate
  upstream connections, one account reuses its own, and an aborted
  connection of one account leaves another account's stream intact.
