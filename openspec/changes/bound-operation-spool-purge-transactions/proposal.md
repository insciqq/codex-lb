## Why

Operation transcript retention deleted each batch of up to 50 operations in
one write transaction, and the rows_v1 transcript of every operation went with
it through the explicit delete or `ON DELETE CASCADE`. Each transcript row
removes an entry at a random position of the event-id index, so the time the
transaction holds SQLite's single writer slot grows with transcript rows, not
with operations. Observed in production on a multi-gigabyte database: a pass
deleting 10 to 19 operations held the writer for 18 to 34 seconds, past the
30-second `busy_timeout`. Every other writer, the leader lease renewal
included, failed with `database is locked`; the lease loss then cancelled
leader-gated work, which kept the lock errors going for many minutes
(upstream issue 1682).

## What Changes

- Selection reads only operation ids, never request or transcript blobs.
- Each selected operation is purged in short transactions: transcript event
  rows at most 256 per transaction, chunk rows at most 32 per transaction,
  then the operation row once no transcript rows remain.
- Every transaction rechecks the existing retention and owner/lease predicate
  with one timestamp shared by its lock and delete, and stops at the first one
  that fails, so an operation whose session is owned again keeps its row.
- Retention deletes issue no `RETURNING`, and the PostgreSQL recheck skips
  rows the request path holds locked instead of waiting in the opposite lock
  order.
- The scheduled pass checks its time budget between committed transactions
  and reports committed deletes when cancelled or failed.
- On the scheduled SQLite path the purge leaves the writer slot free after
  each transaction for as long as that transaction held it after acquiring
  the writer gate (capped at one second), so writers that wait on
  `busy_timeout`, such as leader lease renewal, are not starved while a
  backlog drains. Startup does not pause.

Retention eligibility, the retention window, batch and pass budgets, and
everything sticky-session and prompt-cache affinity keep are unchanged. No
schema change.

## Capabilities

### Modified Capabilities
- responses-api-compat: operation transcript retention bounds each write
  transaction by spool rows.

## Impact

`DurableBridgeRepository.purge_operation_spool_batch` (new optional
`deadline`, `progress` and `pace_writer` arguments, new `budget_exhausted`
result field), the scheduled and startup retention callers, and their
regression tests.
