# Fork policy

This fork tracks [Soju06/codex-lb](https://github.com/Soju06/codex-lb) and
applies selected upstream pull requests that upstream has not merged. It adds
no functionality of its own.

## Branches

| Branch | Content |
|---|---|
| `patched` | Default branch. Upstream `main` plus the patch queue. Deploy only full SHAs from this branch with green CI. |
| `main` | Mirror of upstream `main`. No fork commits. Sync with `git push origin upstream/main:main`. |

Only CI runs in the fork. Release, publishing, docs, labeler, stale and
metadata workflows are disabled in the Actions settings.

Tags named `patched/<date>-<short>` keep previous `patched` heads reachable
after a rebuild. They are recovery points, not versions. The fork publishes
no releases, packages or images.

## What goes into `patched`

* Pull requests opened in upstream Soju06/codex-lb within the fork's scope:
  bug fixes, security, compatibility with current Codex clients and the OpenAI
  backend, dependency updates. No features; the registry records feature PRs
  as `out-of-scope`.
* Own fixes for bugs observed in production, with `Upstream-PR: none` and
  `Upstream-Issue` when an upstream report exists.
* No functionality from other forks and no fork-specific features.
* One fork commit per upstream PR, keeping the PR author as the commit
  author. Squash the PR's commits when it has several.
* No new Alembic revisions: the database schema stays identical to upstream,
  so switching between upstream and the fork is an image swap.

## Commit trailers

Every fork commit ends with trailers. Write numbers without `#`, so pushes
and rebuilds do not add cross-reference events to upstream threads.

| Trailer | Value |
|---|---|
| `Upstream-PR` | Upstream PR number, or `none` |
| `Upstream-PR-Head` | Upstream PR head SHA at pick time |
| `Upstream-Issue` | Upstream issue numbers, comma-separated |
| `Fork-Only` | Reason for a commit without an upstream PR or issue: fork tooling, CI, spec fixes, or an own bug fix found in production or review (`production-fix`) |

`Upstream-PR: none` requires `Upstream-Issue` or `Fork-Only`.

## Report

```bash
git fetch upstream
python3 scripts/fork_patches.py --base upstream/main --head patched
```

The report lists every patch with its upstream PR and issue state. Signals:

* `merged upstream: drop on rebuild` — upstream now contains the fix.
* `closed without merge` / `issue closed upstream` — re-check the patch.
* `head moved` — the upstream PR changed after the pick; review the delta.

## Decisions registry

[fork/REGISTRY.md](fork/REGISTRY.md) records publicly what the fork did with
every upstream PR and issue in scope. `taken` PRs and `fixed` issues come only
from the commit trailers in `base..HEAD`; every other decision (rejected,
superseded, out of scope, not applicable, own fix, merged or fixed upstream,
pending) lives in `fork/decisions.toml`. The document is generated; never edit
it by hand. The data schema and the status rules are in the docstring of
`scripts/fork_registry.py`.

```bash
python3 scripts/fork_registry.py sync            # gh api: add new upstream PRs/issues, refresh, reconcile
python3 scripts/fork_registry.py render          # rewrite the data canonically and the document
python3 scripts/fork_registry.py check           # offline, run by CI
python3 scripts/fork_registry.py check --strict  # before a release
```

CI fails when the data does not parse, is not canonical, has an invalid entry
or contradicts the trailers, and when `fork/REGISTRY.md` is missing or not
committed. A stale document is only a warning; refresh it with `render` when
convenient. `check --strict` also fails on a stale document, on `pending`
entries and on missing or invalid trailers.

Every entry carries a `status`; a taken PR or a trailer-fixed issue stores
`pending`, because its real status comes from the trailers. After a rebuild,
set `[registry].base` to the new upstream `main` and run `sync` and `render`:
`sync` turns a dropped pick that upstream merged into `merged-upstream`,
removes its orphaned `rework` and resets an orphaned issue `partial` to
`pending`. `sync` refreshes what changed upstream since the previous snapshot;
`sync --full` refreshes everything updated since `closed_since`.

## Rebuilding on a new upstream

Upstream squash-merges, so a plain rebase does not recognise merged patches.
Rebuild instead:

1. `git fetch upstream` and run the report against the current `patched`.
2. `git tag patched/<date>-<short> patched` and push the tag.
3. `git switch -C patched upstream/main`, then cherry-pick the patches
   without a `merged upstream` signal, in report order.
4. `git push --force-with-lease origin patched`. CI runs on every push to
   `patched`; a head is deployable only after its own checks are green.

## Considered, not taken

Upstream PRs reviewed and rejected, one line each with the reason.

* 2666: owner-bound replay proof could accept incomplete history (delta fingerprints,
  call-id-only manifest, unchecked parent anchor) and relocate owner content; replaced
  by socket-bound anchoring of native full histories.
* 2472: native give-up terminal; no matching production evidence and several unresolved
  edge cases in review.
* 2101: native history/notes routes; concurrent first writes can pin different owners,
  the stale-session sweep retires history owners, possible session-identity mismatch
  with scoped cache identity; no production evidence of 405s yet.
* 2665: configuration_update effort; enforced-model alias masks disallowed efforts,
  compact trimming drops the authorizing update, model-source egress changes the
  cache-sensitive starting effort.
* 2701: HTTP/2 keepalive pings on the native egress pool; a late PING ack makes the pinned
  Hyper close the shared connection and fail every stream of every account on it (the
  blast radius of issue 2471), and its test covers HTTP/1.1 only.
* 2506: shutdown grace for DB-owning schedulers; a stop can return before the leader
  election registers a detached body, so the lease is released while the old leader's
  work still runs, and fresh post-cancel waits can exhaust the cleanup reserve.
