## Why

The terminal-append bound cancels the append task, but a write inside its
shielded session teardown can absorb that cancellation and keep running past
the bound. Its in-memory context is cleared at the bound, and a newer terminal
attempt for the same operation can register its own context and queue its own
events meanwhile. The old append then read that newer context and wrote its own
terminal outcome under the newer attempt's instance and owner epoch, and a
failed queued-event write of the old attempt dropped the newer attempt's
transcript and queued events.

## What Changes

- A terminal append reads the operation context only while its attempt still
  owns the operation; otherwise it returns the not-persisted,
  settlement-required result without writing.
- Queued-event batches are taken together with the context that owns them, and
  a failed write drops the transcript only while that context still owns the
  operation.

## Impact

`app/modules/proxy/http_bridge_event_batcher.py` and its tests. No setting,
schema or wire-format change.
