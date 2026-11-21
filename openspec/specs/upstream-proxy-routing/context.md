# Upstream Proxy Routing Context

## Purpose and scope

The normative contract is in [spec.md](spec.md). Upstream proxy routing binds
credentialed Codex operations to resolved routes while keeping endpoint
credentials out of errors and request logs. This context focuses on transport
ownership during routed WebSocket setup; account selection and proxy-pool
configuration are outside its scope.

## WebSocket context ownership

aiohttp exposes WebSocket setup as an awaitable asynchronous context manager.
The caller owns its exit path only after entry succeeds. codex-lb therefore
tracks successful entry for each route endpoint independently and never exits a
manager whose connection coroutine or `__aenter__` failed.

This follows Python's `async with` protocol instead of checking aiohttp private
state such as `_resp`. It also keeps successful WebSocket cleanup caller-owned,
so the connection remains open after the routed client returns it.

## Constraints and failure modes

- Connection and handshake errors remain credential-safe and identify only the
  configured endpoint id.
- Existing handshake-status and network-error fallback controls decide whether
  another endpoint is attempted.
- Failed context entry owns any partial-resource cleanup. Calling `__aexit__`
  after failed entry can itself raise and mask the transport failure.
- A successful context must still be returned to the consuming WebSocket layer
  for exactly-once close handling.

## Example

Suppose an HTTP proxy restarts while codex-lb opens an upstream WebSocket.
aiohttp raises `ClientProxyConnectionError` before assigning a response to its
request context. codex-lb classifies that original error and can try the next
endpoint in the resolved route. It does not call the unentered context's exit
method, which would otherwise replace the useful failure with an internal
attribute error.

## Operational notes

No setting or migration controls this lifecycle rule. A proxy outage can still
surface as an upstream-unavailable response after fallback is exhausted, but
operators should see the classified connection failure rather than a cleanup
crash. That distinction allows existing network-recovery and route-health
diagnostics to operate on the actual failure.

## Operations on an account that may be deleted meanwhile

Deleting an account removes its `account_proxy_bindings` row by cascade. An
operation that loaded the account, and holds its tokens in memory, used to
look up the binding afterwards; when the deletion finished in between it saw
no binding and, with global routing off, sent the tokens over direct egress.

`resolve_upstream_route(..., require_account=True)` closes that window. It
reads the account row and its active binding in one statement and raises
`account_not_found` when the row is gone or marked for deletion
(`allow_pending_deletion=True` keeps a deleting row, whose binding still
exists). Force Probe and the dashboard reset-credit endpoints, quota warmup,
targeted OAuth, `AuthManager` token refreshes and `UsageUpdater` usage
fetches use it. The dashboard endpoints answer
404, as when the account was gone on arrival. Quota warmup logs the failure
without an account id, because the request-log foreign key can no longer
point at the row, and skips the account observation.

The default stays lenient (an unknown id reads as "no binding"). The proxy
request path and its per-account route cache, automations, limit warmup, the
cache-isolation probe and model discovery still use it directly; their unit
suites resolve routes for in-memory accounts without rows, and the request
path's failure handling is a separate change. Their token refreshes and usage
fetches go through `AuthManager` and `UsageUpdater` and are strict.

