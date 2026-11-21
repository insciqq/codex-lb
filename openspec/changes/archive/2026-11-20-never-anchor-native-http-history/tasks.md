## 1. Reproduce

- [x] 1.1 Route-level regressions with a connection-scoped fake upstream: idle socket loss (close 1000, 1006, 1011 and 1012), a socket drop with no events followed by the client's retry, a same-account reconnect, a reconnect before dispatch, a stuck-gate replacement and a continuation from an earlier response, each failing before the change.

## 2. Implement

- [x] 2.1 Set the durable-anchor suppression for every native Codex history and skip the fresh-reattach injection under it, keeping the verified-full-resend owner decision.
- [x] 2.2 Record native history on the request state: keep the owner on transport-only replays, admit it through an open retry circuit, refuse the raw HTTP replay for an owner-bound thread, skip the recovery-checkpoint fence, log the large-frame size at DEBUG.
- [x] 2.3 Scope the older full-resend anchoring requirement to requests that are not native history.

## 3. Verify

- [x] 3.1 Scope guards: other clients keep the durable anchor, client-supplied anchors are preserved, an SDK marker is never native, a stale broad owner is still ignored, capacity retries and the ended-session permanent failure behave as before.
- [x] 3.2 Focused bridge suites, ruff, ty, OpenSpec validation; full suites in CI.
