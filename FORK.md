# Fork policy

This fork tracks [Soju06/codex-lb](https://github.com/Soju06/codex-lb) and
applies selected upstream pull requests that upstream has not merged. It adds
no functionality of its own.

## Branches

| Branch | Content |
|---|---|
| `patched` | Default branch. Upstream `main` plus the patch queue. Deploy only full SHAs from this branch with green CI. |
| `main` | Mirror of upstream `main`. No fork commits. Sync with `git push origin upstream/main:main`. |
| `wip/merge-*` | A ready change set on top of `patched`. The only branches pushed for CI. |

Work branches stay local. When a `wip/merge-*` head is green, `patched` is
fast-forwarded to exactly the SHA that CI tested. Take the literal SHA from
the successful run, not a remote ref that may have moved since:

```bash
sha="$(gh run list --workflow CI --branch wip/merge-<name> --event push \
  --status success -L 1 --json headSha --jq '.[0].headSha // empty')" &&
  [[ "${sha}" =~ ^[0-9a-f]{40}$ ]] &&
  git push origin "${sha}:refs/heads/patched"
```

## CI

`.github/workflows/ci.yml` runs the full suite on every push to a
`wip/merge-*` branch. Pushes to `patched` and `main` do not run it:

* Checks belong to a commit, not to a branch. A `patched` head moved to a
  green `wip/merge-*` SHA shows the green checks of that run, so a second run
  would test the same tree again.
* `.github/workflows/patched-ci-guard.yml` runs on every push to `patched`
  and takes seconds. It fails unless a successful full CI run (push, manual
  or merge queue, not pull request) exists for the pushed SHA, so an
  unchecked `patched` head shows red. It stays red until CI for that SHA is
  green and the guard run is re-run. A `patched` head without a guard
  result, for example because a skip-ci directive in the commit message
  suppressed the run, counts as unchecked as well.
* `main` carries no fork commits. A push to `main` still runs the workflows
  defined in the pushed upstream commit, because GitHub reads workflow files
  from that commit.

Run CI by hand on any branch, for example a work branch pushed for an early
signal; a manual run also executes the full suite:

```bash
gh workflow run CI --ref <branch>
gh run list --workflow CI --branch <branch> --limit 1
```

Only CI, the patched CI guard and Fork image run in the fork. Release, publishing, release guard,
simplicity budget, docs, labeler, stale and metadata workflows are disabled
in the Actions settings; they serve upstream's `main`, pull requests and
releases.

Tags named `patched/<date>-<short>` keep previous `patched` heads reachable
after a rebuild. They are recovery points, not versions.

## Releases and images

The fork publishes container images only: no PyPI package, Helm chart or
floating tags. Images are built by GitHub Actions, never locally.

* Version tags are `v<upstream base>-fork.<n>`, for example `v1.25.0-fork.1`
  on upstream's 1.25.0 train. `pyproject.toml`, `app/__init__.py`, `uv.lock`
  and `frontend/package.json` carry the PEP 440 form `1.25.0+fork.1`, which
  the dashboard and `X-App-Version` show. The Helm chart's `version` and
  `appVersion` use `1.25.0-fork.1`, like the image tag, because image tags
  and Kubernetes label values do not allow `+`.
* Pushing a version tag runs `.github/workflows/fork-image.yml`, which
  publishes `ghcr.io/insciqq/codex-lb:<version>` and
  `ghcr.io/insciqq/codex-lb:sha-<full commit SHA>` for linux/amd64 and
  linux/arm64. There is no `latest`, and a tag that already names another
  digest is never moved.
* The workflow runs on the tag ref only. Before building, it requires the
  run's commit to be the tag commit, the commit to be reachable from
  `patched`, a successful full CI run for that exact commit, the tag's
  version in every release-managed file listed above and in the fork
  versions named by the READMEs, `docs/` and the chart README, and that
  `:<version>` is not published yet. Whether a tag exists is read from the
  registry for that exact tag; only HTTP 404 counts as missing, and any
  other answer fails the run.
* It builds once and pushes by digest, signs build provenance for that
  digest, and only then points both tags at it. A re-run of failed jobs
  re-signs or re-tags the same digest without rebuilding.
* The dashboard's update notice compares the running version with
  upstream's latest GitHub release and ignores `+fork.N`. On the 1.25.0
  line an upstream `v1.25.0` is therefore not announced, a later upstream
  release is, and the notice links to upstream's release page.
* A fork release is a version tag plus its GHCR image, and each version tag
  also gets a GitHub Release with release notes: see the fork's
  [Releases](https://github.com/insciqq/codex-lb/releases) and
  [tags](https://github.com/insciqq/codex-lb/tags).
* Images carry `org.opencontainers.image.revision`, `.version` and `.source`.
  The job summary prints the digest.
* Deploy by the `sha-<full>` tag or by digest, not by the version tag, and
  verify the digest first:

  ```bash
  gh attestation verify oci://ghcr.io/insciqq/codex-lb@<digest> \
    --repo insciqq/codex-lb --source-digest <full commit SHA> \
    --signer-workflow insciqq/codex-lb/.github/workflows/fork-image.yml
  ```

* The Helm chart defaults to `ghcr.io/insciqq/codex-lb:<appVersion>`, and
  the README, `docs/` and the chart README name the fork image and release
  tag (`git grep -n 'fork\.[0-9]'`). Bump them with the version; the fork
  publishes no Helm chart or Python package.
* When a run fails in the attest or tag job, finish that run with
  `gh run rerun <run-id> --failed`: it signs or tags the digest already
  built and keeps the earlier verify result. Start a new run with
  `gh workflow run "Fork image" --ref v<version>` only when the run
  published no tag, for example when the workflow was disabled at the tag
  push or the run failed before tagging; a new run stops in verify once
  `:<version>` exists.

The Fork image workflow has to be enabled once in the Actions settings. The
first publish creates the `codex-lb` container package; check in its
settings that it is public before relying on anonymous pulls.

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
`pending`, because its real status comes from the trailers. `sync` sets a PR
that upstream merged to `merged-upstream`; while a branch still carries its
pick it shows as taken with a "drop on rebuild" note. After a rebuild,
set `[registry].base` to the new upstream `main` and run `sync` and `render`:
`sync` turns a dropped pick that upstream merged into `merged-upstream`,
removes its orphaned `rework` and resets an orphaned issue `partial` to
`pending`. `sync` refreshes what changed upstream since the previous snapshot;
`sync --full` refreshes everything updated since `closed_since`.

`deferred` records a decision to act after the first release; it needs a
`reason`. Unlike `pending` it is a decision: `check --strict` does not fail on
it and `sync` keeps it. A taken PR or a trailer-fixed issue cannot be deferred.

## Rebuilding on a new upstream

Upstream squash-merges, so a plain rebase does not recognise merged patches.
Rebuild instead:

1. `git fetch upstream` and run the report against the current `patched`.
2. `git tag patched/<date>-<short> patched` and push the tag.
3. `git switch -C patched upstream/main`, then cherry-pick the patches
   without a `merged upstream` signal, in report order.
4. Push the rebuilt head for CI, `git push origin patched:refs/heads/wip/merge-rebuild-<date>`,
   and wait until it is green. A push to `patched` runs no CI.
5. Move `patched` to the SHA that run tested, not to a ref that may have
   moved:

   ```bash
   sha="$(gh run list --workflow CI --branch wip/merge-rebuild-<date> --event push \
     --status success -L 1 --json headSha --jq '.[0].headSha // empty')" &&
     [[ "${sha}" =~ ^[0-9a-f]{40}$ ]] &&
     git push --force-with-lease origin "${sha}:refs/heads/patched"
   ```

   A head is deployable only after its checks are green; the patched CI
   guard turns red otherwise.

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
* 2634: paused/deactivated accounts' open WebSockets reject every turn, incl.
  full-history retries; no failover or recycle; no production evidence.
* 2555: leave the terminal transcript append running at its bound; superseded by 2600,
  which finalizes the cancelled SQLite cursor that held the writer lock; cancelling at
  the bound is then safe.
