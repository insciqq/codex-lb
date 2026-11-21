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
Account ids are deterministic, so the same happens when the account is
deleted and imported again: the new row has the old id and no binding.

`resolve_upstream_route(..., require_account=True, installation_id=...)`
closes that window. It reads the account row and its active binding in one
statement and raises `account_not_found` when the row is gone, marked for
deletion, or carries another `codex_installation_id` than the loaded row
(the column is fixed for a row's lifetime and kept by in-place credential
replacement). `allow_pending_deletion=True` keeps a deleting row, whose
binding still exists. The default (lenient) mode remains for callers that
resolve without a loaded account (bootstrap OAuth, import-time checks).

Callers and outcomes:

- The proxy request path (`streaming/helpers.py`, shared by HTTP and
  WebSocket Responses, compact, transcription, files, Codex control, proxy
  warmup and realtime) is strict. Its route cache keys entries by account id
  and installation id and does not keep `account_not_found`, so a re-import
  that clears a pending deletion routes again at once. Right after selection
  and refresh, HTTP streaming and the WebSocket connect check the account
  once more and fail over to another account without a health penalty;
  requests that cannot move, and the single-shot paths, answer a retryable
  503 `selected_account_removed` rather than the proxy-outage error.
  The HTTP bridge forced refresh after a 401 owns the selected account's
  lease until a session takes it. Any failure of that refresh or of the
  re-open (deletion, timeout, cancellation) releases it once, and the lease
  goes back before an account health write, so a failed or cancelled write
  cannot strand it. A session start moves a movable request off a deleted
  account and answers `selected_account_removed` for a required owner. A
  reconnect does not reselect: it releases the replacement lease (also under
  a cancellation its cancel scope re-delivers), completes the failed handoff
  and re-raises.
- Force Probe and the dashboard reset-credit endpoints answer 404, as when
  the account was gone on arrival, also when the refresh before them or the
  SQLite redeem claim fails because the row is gone (the claim rows
  reference the account).
- Quota warmup logs without the account id when the row is gone at any
  point, skips the account observation and still settles the decision.
- `AuthManager`, `UsageUpdater`, automations, limit warmup, the
  cache-isolation probe and model discovery are strict.

Unit suites that drive these paths with in-memory accounts, which have no
row, stub the route lookup; real-row coverage lives in
`tests/integration/test_account_diagnostic_routing.py` and
`tests/integration/test_deleted_account_failover.py`.

## Codex usage identity and team seats

Seats of one team workspace share `chatgpt-account-id`, so `/api/codex/usage`
identifies the caller by the identity claims of its bearer token
(`chatgpt_user_id`, `user_id`, `chatgpt_account_user_id`, `sub`), compared
as a set with each seat's stored `chatgpt_user_id` and the claims of its
stored ID token. Candidates are every local seat of the header's account in
any status, plus legacy rows without a stored account id that match the
caller and whose stored ID token names no other account (user and login
claims are the same in every workspace a person belongs to). The claims are not signature-checked: they only select the route of
the caller's own request, upstream validates the token, and a token with
altered claims is no longer a working credential. A token whose own account
claim contradicts the header, or with claims of the wrong type, is rejected
before any request.

The route agreed during validation is checked again before each later
dispatch of the caller's token (the reset-credit consume and the usage
refresh after it). A change fails the consume closed and skips the refresh.

Example: seats A (bound to pool P) and B (unbound) share workspace W. A's
token uses P. If A is paused, its token is rejected before any request,
instead of leaving through B's direct route. A token without a usable seat
claim is sent only if A and B resolve to the same egress; here they differ,
so it fails with 503. When the usage reply names a workspace whose local row
is B, an identified caller A keeps its own seat for the consume and refresh.
