## 1. Implementation

- [x] 1.1 Read the operation context in the terminal append only while its attempt token is current.

- [x] 1.2 Take each queued-event batch with the context that owns it and drop the transcript after a failed write only if that context still owns the operation.

## 2. Verification

- [x] 2.1 `tests/unit/test_http_bridge_event_batcher.py::test_append_left_running_past_bound_does_not_adopt_newer_attempt_context`
  fails without the fence and passes with it.
- [x] 2.2 `tests/unit/test_http_bridge_event_batcher.py::test_failed_drain_of_an_expired_attempt_keeps_the_newer_attempt_transcript`.
