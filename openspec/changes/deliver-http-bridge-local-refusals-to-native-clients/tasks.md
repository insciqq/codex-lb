## 1. Implementation

- [x] 1.1 Keep the unconditional provenance of the denied-anchor fence and the
      retry-circuit suppressions.
- [x] 1.2 Build every other bridge refusal raised before its attempt is sent as
      a candidate through one constructor: the retiring and closed sessions,
      the stream lease refusals, the request-entry metadata lookup, continuity
      lost, owner metadata unavailable, the incompatible admission handoff, the
      recovery-lease fail-closed and the bridge shutdown; mark a refused
      durable claim at the session-creation call site only.
- [x] 1.3 Share a turn-wide, never-cleared marker across every request state
      of the client turn: set it at every `response.create` send and when a
      failed owner forward may have dispatched the turn. In
      `_stream_via_http_bridge`, raise a turn-owned copy of a candidate marked
      as a local pre-dispatch refusal only while the marker is unset.
- [x] 1.4 Leave the failed prewarm on the transport lifecycle.
- [x] 1.5 Record the per-site verdicts, with the reason for every site left on
      the transport lifecycle, in `context.md`.

## 2. Verification

- [x] 2.1 `test_native_codex_http_bridge_local_refusal_returns_error_before_commit`:
      one route case per candidate site. Each returns the refusal's status and
      envelope, proves its site by the layer it left and its log record, sends
      no frame, and returns an empty `200` without the provenance.
- [x] 2.2 `test_native_codex_http_bridge_refusal_after_an_upstream_frame_keeps_the_transport_lifecycle`:
      a failed prewarm, a refusal after an unanswered frame and a refusal after
      a frame upstream rejected end in an empty `200`.
- [x] 2.3 `test_native_codex_http_bridge_local_refusal_emits_terminal_after_commit`:
      a submission refusal and a session-resolution refusal end a committed
      stream with one unmarked `response.failed` and `[DONE]`.
- [x] 2.4 `test_forwarded_session_resolution_refusal_marks_the_owner_error_as_a_local_refusal`
      and `test_native_codex_origin_refusal_after_an_owner_rejection_follows_the_owner_proof`:
      the owner marks its refusal, and the origin delivers a later refusal only
      after a marked owner rejection.
- [x] 2.5 `test_waiters_sharing_one_refusal_get_their_own_turn_verdicts`: two
      turns that receive one shared refusal get their own verdicts, and the
      shared instance is unchanged.
- [x] 2.6 The upstream denied-anchor and retry-circuit tests still pass; pass
      the bridge, owner-forwarding and lease suites, ruff, ty and strict
      OpenSpec validation.
