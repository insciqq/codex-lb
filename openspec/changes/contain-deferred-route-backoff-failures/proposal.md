## Why

A keyed stream defers the route backoff of an account that failed before
dispatch until its API-key reservation settles. When the replacement account
had already delivered `response.completed`, a failing deferred backoff write
raised out of settlement; the stream handler settled again and emitted a
second terminal `response.failed`. In a `finally` path the same failure
replaced a propagating cancellation.

## What Changes

- After settlement or release has committed, a failing deferred route-backoff
  write is logged with its traceback instead of raised. The failed account
  stays queued, so the final cleanup of the request retries it once.
- Cancellation still propagates.

## Impact

Streaming retry only. No setting, schema or API change.
