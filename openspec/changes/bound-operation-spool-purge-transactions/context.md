# Context

## Failure mode

Observed in production on single-instance SQLite (WAL, `busy_timeout` 30 s,
database of several gigabytes, two vCPUs): the operation transcript retention
pass usually finishes in milliseconds, but some passes that deleted 10 to 19
operations took 18 to 34 seconds. Those passes coincided with leader lease
renewal timeouts, cancelled leader-gated tasks and up to half an hour of
`database is locked` from unrelated writers.

## Why a handful of operations is slow

The statements were already indexed. `EXPLAIN QUERY PLAN` on the original
batch:

- selection: `MULTI-INDEX OR` over `idx_http_bridge_operations_state_updated`
  plus a temp B-tree for `ORDER BY updated_at`;
- operation delete: the same index, plus the cascades into
  `http_bridge_operation_events` through
  `idx_http_bridge_operation_events_operation_sequence` and into the chunk
  table through its primary key.

The cost is the volume of a rows_v1 transcript. An operation holds one event
row per streamed SSE block, up to the 2 MiB spool cap, so thousands of rows.
Deleting one row also deletes its entry from the `event_id` (UUID) primary-key
index, at a random position, and from table pages that interleave with
concurrently streamed operations. A synthetic database (3 GB, 4,000
operations, 4 million event rows, eight interleaved streams) from a cold page
cache: purging 10 operations read about 63 MB of pages inside one write
transaction held for 1.6 s, and 40 operations held one for 7.0 s, on local
NVMe. On slower block storage the same random reads take tens of seconds, all
while the transaction holds the writer slot.

## Why the errors outlast the slow pass

A writer slot held past `busy_timeout` fails the leader lease renewal. Lease
loss cancels leader-gated tasks while they are inside SQLite writes. A
cancelled write with `RETURNING` can leave an unfinalized cursor that keeps
the write lock after its connection is closed. The general fix (finalizing
cursors on close) is tracked separately; retention itself no longer issues
`RETURNING` (ORM deletes run with `synchronize_session=False`, which would
otherwise add it). This change removes the trigger: no retention transaction
holds the writer for longer than a bounded number of spool rows, so retention
cannot starve the lease renewal in the first place.

## Decisions

- Bound rows, not operations: the hold time follows rows.
- 256 event rows or 32 chunk rows per transaction. On the synthetic database
  the longest retention transaction from a cold cache dropped from 1.6 s to
  0.10 s for 10 operations and from 7.0 s to 0.09 s for 40 operations.
- One timestamp per transaction for both the lock recheck and the delete, and
  the operation row is deleted only when no transcript rows remain
  (`NOT EXISTS`), so a lease that changes between transactions can never turn
  the operation delete into an unbounded cascade.
- On PostgreSQL the selection and the recheck take their joined row locks with
  `SKIP LOCKED`: `record_operation` locks the session before the operation,
  and waiting in the opposite order deadlocked. A contended operation is in
  use and is left to a later pass, and unlocked operations fill the batch in
  its place.
- The pass time budget is checked after every committed transaction that
  deleted rows. Transactions that find nothing never stop the purge, so once
  a spool is empty the call reaches the row delete: a call always makes
  progress and a later pass resumes the same operation.
- A committed operation delete is counted from the session's `after_commit`
  event, which fires only once the database commit succeeded. Only the
  operation-row commit runs in its own task: a caller cancelled while COMMIT
  executes waits at most five seconds for its outcome, counts a successful
  commit once, then re-raises; a commit still pending after that is cancelled
  and left to the bounded session teardown. Transcript-row commits stay plain.
  A failed or cancelled pass, scheduled or at startup, reports the count.

## Known residual

Other writes in the same leader-gated cleanup pass still use `RETURNING`
(`purge_closed_before`, `purge_abandoned_before`, the retry-circuit purge).
They delete small session-level rows rather than transcripts and are out of
scope here; a cancelled one is covered only by finalizing cursors when their
connection closes.
- Recheck the retention predicate in every transaction. A partially purged
  transcript is never replayed: readers already reject a spool whose bytes or
  sequence do not add up.
- On the scheduled SQLite path, pause after each transaction for as long as
  it held the writer after acquiring the in-process writer gate (at most one
  second). Writers that do not use the in-process writer gate only poll for
  the slot, and a back-to-back loop of short transactions could still starve
  them. Startup has no competing writers and does not pause.
- No index or migration: the plans are indexed, and replacing the UUID
  primary key would rewrite the largest table.

## Example

A pass that selects two expired operations, one rows_v1 operation with 775
event rows and one chunks_v2 operation with 67 chunks, removes these
transcript rows per committed transaction: selection 0; events 256, 256, 256,
7; chunks 0; operation row 0; events 0; chunks 32, 32, 3; operation row 0.
