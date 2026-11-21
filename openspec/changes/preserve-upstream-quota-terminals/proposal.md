# Change: preserve-upstream-quota-terminals

## Why

The three exhausted-retry `response.failed` terminals in
`app/modules/proxy/_service/streaming/retry.py` (the post-refresh transient
terminal, the account-model rejection retry terminal and the outer
retries-exhausted terminal) built their event with
`response_failed_event(code, message, ...)`, whose `resets_at` defaults to
`None`, even though the retained upstream error carries the numeric reset
timestamp parsed out of the upstream envelope. A client that reaches one of
these terminals sees the quota code but not when the window reopens.

## What Changes

- All three exhausted-retry terminals render through
  `_response_failed_event_from_upstream_error`, which forwards the upstream
  `resets_at` (booleans and non-numeric values become `None`) and keeps the
  existing code, message and response id.

No setting, schema, migration, or general failover behavior changes. The
same-account retry budget, account-health penalties, and selection order are
untouched.

### What this deliberately does not do

- **It does not change the failed pre-created HTTP bridge replay terminal.**
  Surfacing the first account's quota terminal after a replacement account
  failed transiently would turn a retryable `stream_incomplete` into a hard
  usage-limit stop; that terminal is to be derived from the pool-walk
  exhaustion probe instead.
- **It does not add `resets_in_seconds`** to `response.failed`; only
  `resets_at` is part of the event contract.

## Impact

- Code: `app/modules/proxy/_service/streaming/retry.py`.
- Clients: an exhausted-retry stream terminal keeps the upstream reset hint.
- Tests: `tests/unit/test_proxy_utils.py`,
  `tests/integration/test_proxy_transient_retry.py`.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `responses-api-compat`: exhausted-retry stream terminals preserve reset
  metadata.
