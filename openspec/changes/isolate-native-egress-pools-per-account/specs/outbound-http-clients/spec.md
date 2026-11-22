## ADDED Requirements

### Requirement: Native HTTP connections are isolated per account

Native Responses streaming, compact, and account-probe HTTP requests, direct
and routed, MUST carry an opaque per-account pool key derived from the
load-balancer account id, and the native helper MUST NOT send requests with
different pool keys over the same upstream connection. The key MUST NOT be
derived from the upstream account id, which several load-balancer accounts
can share, and MUST NOT contain an account identifier, token, or email in
clear. Requests with the same key and connector policy MUST reuse the same
client pool. A request without a key MUST use the shared client pool, so an
adapter that does not send the key keeps the previous behavior. The helper
MUST evict a cached client that no request uses only after it has been unused
for longer than the connection idle timeout, MUST keep at most a fixed number
of unused clients, counting the client being leased, by evicting the least
recently used first, and MUST NOT evict a client while a request uses it. The
compact connect timeout MUST be rounded down to a 100 ms step, or to whole
milliseconds below one step, so the time already spent does not give each
compact request its own client. Native HTTP request and connect timeouts MUST
be sent as whole milliseconds no greater than the remaining budget, except
for the 1 ms protocol minimum. A helper that honors the pool key MUST
advertise `http_pool_key_v1`; the adapter MUST NOT require it and MUST log
one warning per helper process when it is missing. WebSocket connections are
not affected by this requirement.

#### Scenario: Connection failure stays within one account

- **GIVEN** two accounts stream Responses over native HTTP at the same time
- **WHEN** the upstream connection of one account fails
- **THEN** only that account's in-flight streams fail
- **AND** the other account's streams continue on its own connection

#### Scenario: Seats of one workspace get separate pools

- **GIVEN** two load-balancer accounts share one upstream account id
- **WHEN** both send native HTTP requests, including warmups and failover
  attempts
- **THEN** each request carries the key of its own load-balancer account

#### Scenario: One account reuses its own connection

- **WHEN** an account sends sequential native HTTP requests with the same
  connector policy
- **THEN** they reuse that account's pooled connection
- **AND** a request for another account or without a key does not reuse it

#### Scenario: Unused clients are evicted

- **GIVEN** a cached client that no request uses
- **WHEN** it has been unused for the client lifetime, or the number of
  unused clients exceeds the bound and it is the least recently used
- **THEN** the helper evicts it on a later client lookup
- **AND** a client with a request in flight is kept

#### Scenario: Older helper is visible but still works

- **GIVEN** a helper whose hello lacks `http_pool_key_v1`
- **WHEN** the adapter starts it and sends native HTTP requests
- **THEN** the handshake succeeds and the requests are served
- **AND** one warning per helper process says per-account connection pooling
  is unavailable until the helper is rebuilt

#### Scenario: Pool key carries no account identity

- **WHEN** the adapter sends a pool key to the helper
- **THEN** the key is a digest that does not contain the account id
