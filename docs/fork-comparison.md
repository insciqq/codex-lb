# Other codex-lb forks

Several public forks of [Soju06/codex-lb](https://github.com/Soju06/codex-lb)
carry fixes on top of upstream. This page records how three of them compare
with this fork, which of their fixes this fork took or fixed its own way, and
which it did not take and why.

Plain numbers on this page are upstream Soju06/codex-lb pull requests and
issues. What this fork did with each upstream pull request and issue in scope
is recorded in [fork/REGISTRY.md](https://github.com/insciqq/codex-lb/blob/main/fork/REGISTRY.md).

## Scope and method

The comparison was made on 2026-11-22 against these refs:

| Fork | Refs compared |
|---|---|
| [Frozen811/codex-lb](https://github.com/Frozen811/codex-lb) | `main` at [9d22321](https://github.com/Frozen811/codex-lb/commit/9d22321892602f2a762e166894bc63a7a74aa41b) |
| [mikomiel/codex-lb](https://github.com/mikomiel/codex-lb) | `main` and `candidate/serious-fixes-2` |
| [Lok3rn3t/codexLB-up](https://github.com/Lok3rn3t/codexLB-up) | `codexLB-up` up to `v1.26.0-beta.3`, plus its open candidate branch |

For each fork, the commits beyond upstream `main` were read as diffs, and every
fix was checked against this fork's `main`. Their builds and tests were not
run. Other forks in the codex-lb network were not reviewed. The figures below
describe the state on that date; the forks and this fork have moved since.

## Overview

| | Frozen811 | mikomiel | Lok3rn3t |
|---|---|---|---|
| Base | upstream `main` of 2026-09-24, 48 commits behind | current upstream `main` | current upstream `main` |
| Size | 124 commits, mostly large batches; last push 2026-10-15 | 21–26 commits, no releases | about 10 substantive patches |
| Schema | 13 new Alembic revisions; 29 existing upstream revisions edited | 1 new revision (PR 2686) | unchanged |
| Overlap with this fork | every upstream PR or issue it references has a decision in the registry; 31 of the 68 upstream PRs ported here were opened after its last push | ports 15 upstream PRs: 13 are here, the bug behind PR 2575 is fixed here differently, PR 2686 needs a migration; carries about 13 of the 68 | every fix but one is here, as the same upstream PR (including PR 2682) or as a different fix for the same bug; carries about 13 of the 68 |

This fork keeps the database schema identical to upstream, so switching
between upstream and this fork is an image swap. A database migrated by a fork
that adds or edits Alembic revisions cannot be switched back that way.

Frozen811 also bundles features, such as MySQL/MariaDB, quota pools and PAT
accounts, and takes upstream PRs that this fork declined or deferred. Its
`ISSUES.md` states that its ✅ marks are claims pending an independent audit.

### Where the approaches differ

* **Context replay (2574, 2730, 2582).**
    * Lok3rn3t's fork replays a client's full resend without its anchor when
      the input covers the anchor context. mikomiel's port of PR 2575 allows a
      replay without the anchor after an empty prewarm.
    * In this fork a WebSocket delta is never replayed without an anchor the
      socket completed, and a stale client anchor gets
      `previous_response_not_found`
      ([41d7ebf](https://github.com/insciqq/codex-lb/commit/41d7ebfc98268e1b0076aa5843dfe5883c54d2a9),
      [b9cee93](https://github.com/insciqq/codex-lb/commit/b9cee93ba7b96fdbeda8be713150dd046c43ea0b),
      [cdb2ed1](https://github.com/insciqq/codex-lb/commit/cdb2ed1c7120b76ce66714de2a2a45da1cf2195f)).
    * Native Codex HTTP history is never anchored
      ([cc4c73d](https://github.com/insciqq/codex-lb/commit/cc4c73dff93c4de7f70504dbff304622ce1a675d)).
      For native Codex this removes the class of failure that PRs 2581 and
      2628 work around.
* **Idle context loss (PR 2636).** Lok3rn3t's port sends the 1012 close only
  after a completed response. This fork also sends it after an interrupted
  one, which Codex chains on
  ([450b88b](https://github.com/insciqq/codex-lb/commit/450b88b63a39083cbf0439c1e082c36e8d38559b)).
* **Native WebSocket bursts (2594).** Lok3rn3t uses PR 2711, credit-based flow
  control that changes the Rust protocol. This fork uses PR 2635, a bounded
  queue of 4096 events or 32 MiB with an account-neutral error on overflow.
* **Cross-replica owner forwarding (2655).** mikomiel encodes only namespaced
  affinity keys and strips whitespace from raw ones. This fork encodes any key
  a header cannot carry verbatim and checks the raw value exactly as received
  ([a8d12fa](https://github.com/insciqq/codex-lb/commit/a8d12fa1092624b9ab07f4acd55eb06807497a8c)).
* **SQLite.** mikomiel's SQLite fix is PR 2600 alone. This fork adds bounded
  transcript-purge transactions on top.
* **Credit limits (2556).** mikomiel's PR 2686 adds a migration and its own
  credit price table. This fork does not take it, so `credits` limits still do
  not accrue; the registry records 2556 as not fixed.
* **Taken by Frozen811, declined here.** Examples are PRs 2122, 2504 and 2444,
  which need new Alembic revisions, and PR 2548, which removes the 12-hour
  limit on remote admin dashboard sessions. The registry gives the reasons.

## Fixes found in other forks

The comparison found the bugs below fixed in another fork and not, or only
partly, in this one. Each row records what this fork decided. "fork.3"
is the third fork release on the upstream 1.25.0 base, prepared after the
comparison.

| Problem | Fixed in | Decision here |
|---|---|---|
| The HTTP bridge keeps an upstream socket past OpenAI's 60-minute limit, causing an occasional 400 `websocket_connection_limit_reached` (2658) | mikomiel [4dd5657](https://github.com/mikomiel/codex-lb/commit/4dd5657c3579ac221d8ee9e5ce1f0064752307ab) | Not in fork.3; deferred. The bridge does not renew upstream sockets yet. Three designs were reviewed (renewal between self-contained turns, renewal with replay, and retiring a refused socket with a retryable error); each had races with queued turns, durable recovery checkpoints or detached clients. Tracked in [issue 6](https://github.com/insciqq/codex-lb/issues/6) for a later release. |
| A streaming turn that ends incomplete, failed or interrupted releases its API-key reservation even when upstream reported usage, so per-key token and cost limits undercount (no upstream issue) | Lok3rn3t [0312837](https://github.com/Lok3rn3t/codexLB-up/commit/03128371d370bbe444c49f2393515c1a7f9e1404) | Not in fork.3; deferred. Per-path charge rules overcharged on other paths in review; the fix needs one charge decision per request ([design](#api-key-usage-design), [issue 3](https://github.com/insciqq/codex-lb/issues/3)). |
| Other HTTP-bridge local refusals (bridge closed or retiring, owner lookup unavailable, continuity lost) reach native Codex as an empty 200 (2388) | Frozen811 [e03f212](https://github.com/Frozen811/codex-lb/commit/e03f212a1350b71c1a7d5badd065d7dc0e9d6b45) | Partly fixed in fork.3: the bridge's own refusals are delivered while no frame of the turn has been sent; refusals after a sent frame stay conservative. ([issue 5](https://github.com/insciqq/codex-lb/issues/5)) |
| The native HTTP egress shares one HTTP/2 connection across all accounts, so one reset fails every account's streams (2471) | Frozen811 [cf0455a](https://github.com/Frozen811/codex-lb/commit/cf0455a17ceb253b9657f50d66aa790c87aae102) | Fixed in fork.3 with per-account connection pools. 2471 stays partial for retry before response headers and WebSocket headroom. ([issue 7](https://github.com/insciqq/codex-lb/issues/7)) |
| A hard `CODEX_SESSION` owner outside the API key's assigned accounts waits for owner recovery instead of failing fast (no upstream issue) | Frozen811 [5fd5b81](https://github.com/Frozen811/codex-lb/commit/5fd5b8141a13bde635ba0b38b283c6c81009a4ee) | Fixed in fork.3 for HTTP, WebSocket, the HTTP bridge and owner forwarding. ([issue 4](https://github.com/insciqq/codex-lb/issues/4)) |
| `database is locked` on dashboard sign-in under SQLite load (2447) | Frozen811 [0170f48](https://github.com/Frozen811/codex-lb/commit/0170f487daf7cbc103b58bd33955979bc2dd09b0) | Partly fixed in fork.3: rate-limiter, last-login, TOTP step and OIDC flow writes queue behind the SQLite writer section. Identity-resolver and credential writes remain. ([issue 8](https://github.com/insciqq/codex-lb/issues/8)) |
| A deeply nested pass-through extension field returns 500 instead of 400, because the JSON depth guard checks only named fields | Frozen811 [ba45123](https://github.com/Frozen811/codex-lb/commit/ba4512387d71ba51cf1f6c27e0b5303364bcfa36) | Fixed in fork.3. ([issue 9](https://github.com/insciqq/codex-lb/issues/9)) |
| Owner retirement waits for a leftover reset time for every unavailable status | Frozen811 [0170f48](https://github.com/Frozen811/codex-lb/commit/0170f487daf7cbc103b58bd33955979bc2dd09b0) | Fixed in fork.3: a reauth owner whose token still works is kept. ([issue 9](https://github.com/insciqq/codex-lb/issues/9)) |
| An owned terminal is lost in the post-submit cooldown check | Frozen811 [e684963](https://github.com/Frozen811/codex-lb/commit/e684963f7d6eafa86727447fc636e1726348c22a) | Fixed in fork.3, independently of that fork's incomplete-response counting, which this fork does not take. ([issue 9](https://github.com/insciqq/codex-lb/issues/9)) |
| Non-finite `resets_at` values (PR 2440) | Frozen811 [6ca0f6f](https://github.com/Frozen811/codex-lb/commit/6ca0f6f059a9dfaf26b69a582f2db7e2f75469c5) | Not taken: upstream does not send non-finite numbers, and each partial guard left other relay paths unguarded. To be revisited if observed. ([issue 9](https://github.com/insciqq/codex-lb/issues/9)) |
| `credits` limits never accrue usage (2556) | mikomiel [c20235d](https://github.com/mikomiel/codex-lb/commit/c20235d148b42cf3d0f5f7798f8e97082e5854b7) (PR 2686) | Not taken: it needs a schema change and its own pricing table. |

Bug fixes first written in another fork are welcome here as issues or pull
requests, within the fork's scope
([Pull requests to this fork](https://github.com/insciqq/codex-lb/blob/main/FORK.md#pull-requests-to-this-fork)).

## Design: API-key usage on non-success turns {#api-key-usage-design}

This is the design for the deferred API-key usage fix in the table above
([issue 3](https://github.com/insciqq/codex-lb/issues/3)).

### Problem

Stream settlement charges an API-key reservation only for a turn settled as
`success`. Turns that end with upstream `response.incomplete`,
`response.failed` or a controlled interruption, for which upstream reports
usage, release the reservation instead, so per-key token and cost limits
undercount. Plain HTTP/SSE also never reads usage from `response.failed`.

### Why per-path rules miss paths

* The charge decision has no owner. Usage is copied onto a mutable settlement
  (HTTP) or captured per frame (WebSocket and HTTP bridge) as soon as an
  upstream terminal event is parsed, and whether it is charged depends on
  which later path calls settle.
* Many paths reach that state: retry and failover handlers, security-work
  advisories, bridge replay branches, stale-anchor recovery, rewrites,
  cancellation, the consumer's `aclose()`, outer cleanup, deferred-penalty
  drains, and grouped or anonymous terminal matching.
* Each per-path fix is subtractive: it clears usage on one path, and every
  path not listed inherits whatever is still attached. Charging by default
  with opt-outs cannot be shown to be complete.
* Usage lives in the wrong scope. Attempt and frame objects outlive the
  attempt, a forwarded original event survives a rewrite, and a terminal
  event can be matched to a different pending request.

### Proposed design: one charge decision per request

1. Each keyed request has one request-scoped charge record that holds the
   reservation. Each upstream terminal event produces an immutable
   attempt-usage value, held by the attempt that parsed it.
2. There is a single commit point. Only the code that delivers the request's
   ending terminal event to this request's client may commit, after checking
   that the attempt is the current one and that the terminal was matched to
   this request rather than inferred anonymously. The commit is idempotent and
   compare-and-set.
3. Every other exit releases by default: retry, replay, failover, reroute,
   recovery, cancel, `aclose()`, `finally`, penalty drain, grouped or
   anonymous terminals, detach and shutdown. A missed commit can only
   undercount, never overcharge.
4. Attempt identity is fixed at dispatch. A retry or replay advances the
   current attempt before any `await` or `yield`, so a stale terminal cannot
   commit.
5. A rewrite of the ending terminal event keeps its attempt; the commit uses
   that attempt's own reported usage, never a forwarded or synthesized event.
6. Request logs stay per attempt.
7. Usage counts when upstream reports both input and output tokens, zeros
   included. An attempt the request moved away from is never charged. When the
   outcome is ambiguous, the reservation is released.

### Acceptance scenarios

In these scenarios, 40/7 means upstream reported 40 input and 7 output tokens
for the attempt; A and B are attempts or pipelined requests.

1. HTTP failover where the replacement fails the freshness check: the
   reservation is released, and A's request log shows 40/7.
2. A WebSocket or HTTP-bridge terminal rewritten by stale-anchor recovery or
   duplicate-tool suppression, with 40/7: finalized at 47.
3. A bridge capacity replay is staged and the replacement gets `no_accounts`:
   released.
4. A security-work refusal that is not retried, with 40/7: finalized at 47.
5. The client calls `aclose()` at the security-work advisory after A's
   deferred penalty: released, and A's penalty is kept.
6. Bridge stale-anchor recovery: only the ending attempt is charged (52, not
   99).
7. An anonymous terminal event after A detaches does not charge the pipelined
   sibling B.
8. A charge that was explicitly discarded is not taken from the forwarded
   original event in the WebSocket finalizer.
9. A cancel during `inner_stream.aclose()` of an attempt selected for retry:
   released.

## See also

* [fork/REGISTRY.md](https://github.com/insciqq/codex-lb/blob/main/fork/REGISTRY.md):
  the decision and reason for every upstream pull request and issue in scope.
* [FORK.md](https://github.com/insciqq/codex-lb/blob/main/FORK.md): the fork's
  policy, branches, releases and how pull requests land.
