## 1. Incident and regression

- [x] 1.1 Collect read-only production request timeline, configured caps, and deployed source hashes; record evidence and separate unproven causes in design.md.
- [x] 1.2 Reproduce incorrect warm admission with actual balancer leases at both public HTTP Responses routes before production-code edits; verify failures on main and matching deployed source.

## 2. Admission fix

- [x] 2.1 Thread one typed reacquire snapshot through both locked admission checks; verify keyed/unkeyed higher, lower, unlimited, inherited, partitioned, and updated cap behavior.
- [x] 2.2 Preserve no settings I/O under pending locks, already-held lease fast path, and cancellation/failed-read cleanup; verify partial-failure and lease-ownership regressions.
- [x] 2.3 Refuse a reacquisition that has no snapshot with 503 `upstream_unavailable` instead of asserting.

## 3. Verification and handoff

- [x] 3.1 Synchronize stable admission requirements/context and run focused route/bridge/balancer suites, lint, typing, and strict OpenSpec validation; record actual results.
- [x] 3.2 Obtain independent read-only review and validate patch compatibility with the deployed candidate source; deliver root cause, verified patch, and explicit rollout/rollback proposal without changing production.
