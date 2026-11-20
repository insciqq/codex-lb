## Why

The bounded managed HTTP client close waits up to 10 seconds inside the same
post-drain cleanup reserve (25 seconds by default) as the leader-lease
release (10 seconds), the metrics-server stop (5 seconds) and database
disposal (`close_db()`, whose wedged-teardown drain is bounded at 10
seconds). In the worst case those waits add up to 35 seconds, so the
graceful server's force-exit can pre-empt `close_db()` and the next startup
runs the integrity scan, and the response.create dump settlement that runs
after `close_db()` gets no time at all.

## What Changes

- Reserve the response.create dump margin first, then bound the HTTP
  client close, the metrics-server stop and the `close_db()` teardown-registry
  drain against that one cutoff: each later step keeps its own bound, and the
  HTTP close stays capped at the previous 10 seconds. Without a committed
  shutdown deadline the previous bounds apply.
- `close_db_teardown_wait_seconds()` bounds only the registry drain, not
  `engine.dispose()`.
- Keep the order: HTTP client close, metrics-server stop, process-death
  marking, then database disposal.

## Impact

- Affected capability: `graceful-shutdown`.
- Affected code: `app/main.py`, `app/db/session.py`.
