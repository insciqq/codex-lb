## Verification report

Base: `3d788af3`. Implementation: the four production files from #2486,
plus its selector regressions and new native HTTP-route coverage.

| Dimension | Result |
| --- | --- |
| Completeness | Six tasks complete; one requirement and four scenarios covered |
| Correctness | Original-owner recovery, sibling availability, real quota/status refusal, and retained health evidence verified |
| Coherence | Existing selection fallback reused; no new retry loop, setting, migration, or replay policy |

## Implementation and scenario evidence

- `app/core/balancer/logic.py::select_account` admits transiently backed-off
  candidates only through the existing fallback when `hard_owner_pool` applies.
  Sticky and unbound selection propagate the resolved-owner condition.
- `test_http_bridge_recovers_owned_full_resend_during_owner_backoff` (in this
  fork; upstream's version is
  `test_http_bridge_recovers_owned_full_resend_after_anchor_denial_and_backoff`)
  covers both `/v1/responses` and `/backend-api/codex/responses` using native
  CLI headers, real API-key authentication, SQLite, selection, and health writes.
  It verifies a working sibling session, unchanged encrypted reasoning and
  completed tools on the original owner, exactly one successful response
  lifecycle, and a subsequent turn-state continuation. See "Fork port" below
  for what it covers compared with upstream's test.
- Its real-quota variants refuse the original owner and never send its opaque
  context to the sibling. Selector contract tests additionally cover paused
  accounts, file ownership, caller exclusions, scope, and policy restrictions.
- Core/selector regressions verify selection does not erase error counters or
  timestamps and that soft preferences can still select a healthy sibling.

## Validation

- Baseline experiment: the new positive native backend case fails with HTTP
  502 instead of 200 when the four production files are restored to main.
  The patched files were restored after that experiment.
- Four new native HTTP cases pass, including a frozen selector clock to keep
  the owner inside transient backoff throughout slow runs.
- Selection suites: 417 passed (three unit modules plus load-balancer integration).
- HTTP bridge and direct WebSocket integration: 359 passed; the Windows baseline
  idle-reader test listed below was excluded.
- Ruff lint/format and affected-file typing pass. Proxy architecture,
  cancellation safety, timing seams, settings tiers, and migration topology pass.
- Strict change validation passes; all 67 main specs validate.
- Main sticky-session requirements and context include the delta requirement.

## Recorded limitations

- `test_v1_responses_http_bridge_idle_recovery_hands_reader_to_replacement`
  times out on Windows on both baseline and modified code; excluded from the
  transport run. Linux CI still needs to execute it.
- Whole-repository typing reports 14 existing Windows diagnostics involving
  Linux-only imports/attributes; affected-file typing is clean.
- Full local CI, PostgreSQL, packaging, and live upstream execution were not
  run. Cloud results must be checked independently before readiness/merge.
- This fixes owner selection after transient failures. It does not fix the
  initiating upstream failure, #2493's native error delivery, WebSocket
  required-owner exclusion (#2591), every poisoned-anchor alias path, actual
  quota exhaustion, or automatic-reviewer authentication.
- No production container restart, shared production DB test, or client config
  change was performed. This report does not claim live deployment success.

No critical implementation/spec gaps found for this scoped change. The
environmental limitations above remain explicit validation warnings.

## Fork port

This change was ported with two differences from the upstream pull request.

- **Ownership narrowing.** Upstream set `hard_owner_pool` whenever
  `required_account_id` was present. That also covered a soft preferred
  account, single-account routing, and reuse probes that disable fallback
  only to test the current account (the open-WebSocket revalidation and the
  HTTP bridge reconnect that reuses its stream lease). The fork sets it only
  for a resolved hard sticky owner or a required continuity owner.
  Regressions in `tests/unit/test_load_balancer_contract.py` cover the owner,
  hard sticky, soft preference, single-account and reuse-probe cases at the
  balancer, at service selection, and through the WebSocket revalidation and
  HTTP bridge reconnect paths. An input-file owner gets the bypass when an
  existing bridge session reconnects; a file pin on first bridge session
  creation is not continuity evidence and fails closed during backoff, as
  upstream does.
- **Route test.** The fork never anchors native Codex HTTP history, so the
  anchor-denial step of upstream's route test cannot occur. The fork's test
  sends one native turn, puts the owner into transient backoff, serves a
  separate thread on the sibling, and then sends a complete owner-only
  resend. It covers owner recovery, unchanged history, turn-state
  continuation and real-quota refusal as upstream's does, but not anchor
  denial or cleared anchor metadata. Without the selection change it fails
  with 502 instead of 200.
