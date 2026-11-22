# Contributing to codex-lb

Thanks for thinking about contributing to codex-lb! This document covers
everything you need to get from "clone" to "merged PR" without re-discovering
the conventions yourself.

If anything below is unclear or out of date, please open a small `docs:` PR —
that's the easiest first contribution.

This repository is a maintained fork of
[Soju06/codex-lb](https://github.com/Soju06/codex-lb). Its scope, branches,
CI, releases and required commit trailers are in [FORK.md](../FORK.md); where
FORK.md differs from this document, FORK.md applies to pull requests here.
Issues and pull requests within the fork's scope are welcome here from anyone;
[Pull requests to this fork](../FORK.md#pull-requests-to-this-fork) says how
they are reviewed and land.

---

## Table of contents

1. [Code of conduct](#code-of-conduct)
2. [Ways to contribute](#ways-to-contribute)
3. [Development setup](#development-setup)
4. [Project layout](#project-layout)
5. [Workflow: OpenSpec-first](#workflow-openspec-first)
6. [Coding conventions](#coding-conventions)
7. [Commit & PR conventions](#commit--pr-conventions)
8. [Merge gates and collaborator rules](#merge-gates-and-collaborator-rules)
9. [Tests](#tests)
10. [Release process](#release-process)
11. [Security issues](#security-issues)

---

## Code of conduct

This project does not ship a formal code of conduct document. Be respectful
and constructive in issues, discussions, and reviews; maintainers may moderate
or remove content that is not.

## Ways to contribute

You don't need to write code to help:

- **Triage**: reproduce open issues, ask for missing details, add labels.
- **Docs**: improve the README, fix typos, document a deployment recipe.
- **Discussions**: answer "how do I…?" questions in
  [GitHub Discussions](https://github.com/insciqq/codex-lb/discussions).
- **Bug reports**: file a structured report using the Bug Report issue form.
- **Feature ideas**: new features are on hold until the fork has settled with
  a few stable releases; propose them in Discussions first if open-ended, and
  use the Feature Request form when the idea is concrete.
- **Code**: fix a bug or a security problem, keep codex-lb compatible with
  current Codex clients and the OpenAI backend, update a dependency.

Look for issues labelled
[`good first issue`](https://github.com/insciqq/codex-lb/issues?q=is%3Aopen+is%3Aissue+label%3A%22good+first+issue%22) or
[`help wanted`](https://github.com/insciqq/codex-lb/issues?q=is%3Aopen+is%3Aissue+label%3A%22help+wanted%22)
if you're looking for a starter task.

## Development setup

codex-lb is a Python project managed with [`uv`](https://docs.astral.sh/uv/).

```bash
# 1. Fork & clone
git clone https://github.com/<your-user>/codex-lb.git
cd codex-lb

# 2. Install Python + deps via uv
uv sync --all-extras --dev

# 3. Activate the venv (optional — `uv run` works without activating)
source .venv/bin/activate

# 4. Install pre-commit hooks
uv run pre-commit install

# 5. Run the proxy locally
uv run codex-lb --help
```

Frontend (dashboard) lives under `frontend/`. Use the project's standard
package manager (see `frontend/package.json`).

## Project layout

```
.
├── app/                  # Python application (proxy, accounts, dashboard backend)
├── frontend/             # Dashboard SPA
├── deploy/helm/codex-lb/ # Helm chart
├── tests/
│   ├── unit/             # Fast, isolated tests
│   └── integration/      # End-to-end / network-touching tests
├── openspec/             # SSOT — specs, change proposals, archived changes
│   ├── specs/<capability>/
│   └── changes/<change>/
└── .github/              # Issue forms, PR template, workflows
```

## Workflow: OpenSpec-first

codex-lb uses **OpenSpec as the source of truth** for change-driven development.
You don't have to author OpenSpec entries for trivial fixes, but for anything
that changes observable behavior, requirements, contracts, or schema, an
OpenSpec change is expected.

The typical flow:

1. Find the relevant spec in `openspec/specs/<capability>/spec.md`.
2. If your change alters behavior: create
   `openspec/changes/<kebab-case-change>/` with a proposal + tasks.
3. Implement the tasks; keep `spec.md` in sync.
4. Validate: `openspec validate --specs`.
5. Verify and archive when done.

If you're unsure whether your PR needs an OpenSpec change, open it as draft
and ask — a maintainer will tell you.

**Do not** hand-edit `CHANGELOG.md`; it is upstream's. The fork's release
notes are on its GitHub Releases.

## Coding conventions

- **Python**: 3.13+. Code is formatted with `ruff format`, linted with
  `ruff check`, and type-checked with `ty` (Astral's type checker).
- **Imports / style**: follow what you see in nearby files; the linter is
  authoritative — if `ruff` and your style guide disagree, ruff wins.
- **Type hints**: required on new public functions and new modules. Existing
  code is being typed incrementally.
- **Logging**: prefer the project's structured logger over `print`.
- **Secrets**: never commit API keys, tokens, account credentials, or `.env`
  files. The CI redaction is best-effort, not a safety net.

Run the full lint/test gate locally before pushing:

```bash
uv run pre-commit run local-ci --hook-stage manual --all-files
```

The `local-ci` hook runs `make ci`, which is the local
version of the GitHub Actions CI gate. You can also run a single CI job while
iterating, for example:

```bash
make lint
make test-unit
make package
```

## Commit & PR conventions

codex-lb uses **[Conventional Commits](https://www.conventionalcommits.org/)**.
The fork's versions follow [FORK.md](../FORK.md#releases-and-images), not
the commit types.

Format:

```
<type>(<scope>)?: <subject>

[optional body]

[optional footer(s)]
```

Common `type`s:

| Type        | When to use it                                            |
|-------------|-----------------------------------------------------------|
| `fix`       | Bug fix                                                   |
| `feat`      | New user-facing feature (on hold in this fork)            |
| `refactor`  | Internal change, no behavior change                       |
| `perf`      | Performance improvement                                   |
| `docs`      | Documentation only                                        |
| `test`      | Test-only change                                          |
| `build`     | Build system / packaging                                  |
| `ci`        | CI configuration                                          |
| `chore`     | Tooling / maintenance with no user-visible effect         |

Common `scope`s used in this repo: `proxy`, `accounts`, `auth`, `oauth`,
`quota`, `chat`, `proxy-responses`, `dashboard-auth`, `ui`, `db`, `types`,
`openspec`, `fork`.

**Breaking changes**: append `!` after the type/scope and/or add a
`BREAKING CHANGE:` footer.

Examples (from the actual repo history):

```
fix(proxy): handle model fetch timeouts
fix(accounts): recover quota status from usage refresh
feat(proxy): add OpenAI-compatible /v1/images API (gpt-image-2 via image_generation tool)
fix(dashboard-auth): reject passwords longer than bcrypt's 72-byte limit
```

PR titles must follow the same format.

### Pull request flow

1. Create a branch from `main`: `git checkout -b <type>/<short-name>`.
2. Make atomic commits with Conventional Commit titles.
3. Run the lint/test gate locally (see above).
4. Open a PR using the template. Link the relevant issue.
5. CodeRabbit (and a human maintainer) will review. Address feedback by
   pushing follow-up commits — no force-pushing during active review.
6. Once approved and CI is green, a maintainer lands the change on `main` as
   one commit with you as its author and closes the PR with a link to it
   (GitHub's merge button is not used; see
   [FORK.md](../FORK.md#pull-requests-to-this-fork)).

## Merge gates and collaborator rules

These rules apply to **every PR**, regardless of author (external
contributor, project owner, or collaborator). They formalize the review
bar that has produced the current proxy / continuity-recovery / OpenSpec
quality so far.

### Merge gates

Before a PR lands on `main`:

1. **CI must be all-green** on the merge-target head. "UNSTABLE, looks
   fine" is not a green CI; rerun, fix, or wait. The Helm / migration /
   PostgreSQL test jobs are part of the gate, not optional. In this fork
   a change lands only when the push run of CI on its `wip/merge-*` branch
   is green for exactly the SHA that `main` is fast-forwarded to. Separately,
   the main CI guard turns a `main` head red unless some successful full CI
   run (push, manual or merge queue) exists for that SHA
   ([FORK.md](../FORK.md#ci)).
2. **Actionable CodeRabbit findings must be fixed or explicitly addressed
   or dismissed in-thread on the merge-target head.** Review the current-head
   CodeRabbit findings before merging; no finding may be silently skipped.
   Local `codex review --base origin/main` runs remain an encouraged extra
   tool, but they are not a merge gate and do not substitute for CodeRabbit.
   - **P1 findings**: fix in the PR, or justify in-thread with a short
     write-up of why the finding doesn't apply. No silent skipping.
   - **P2 findings**: fix in the PR, or open a follow-up issue and link
     it in the PR thread before merging.
3. **`mergeStateStatus` must be `CLEAN`** in the GitHub API — no merge
   conflicts, no requested-changes review still outstanding, no missing
   required status check.
4. **OpenSpec change folder for behavior changes** (see
   [Workflow: OpenSpec-first](#workflow-openspec-first)). Pure
   refactors, docs-only edits, dev-tool changes, and test stabilization
   PRs are exempt; everything else needs an `openspec/changes/<slug>/`
   entry.
5. **`Fixes #N` / `Closes #N` in the PR body** for anything that
   resolves an issue, so the change stays traceable. The PR is closed rather
   than merged, so the maintainer closes the issue when the change lands.
   Use `Refs #N` / `Related to #N` for partial cover.
6. **Simplicity gates must pass** (see
   [Simplicity gates](#simplicity-gates)): the six simplicity rules
   (PRINCIPLES.md P1-P6). Budget exceptions need the
   maintainer-applied `simplicity-budget-approved` label.

### Simplicity gates

These implement [PRINCIPLES.md](../PRINCIPLES.md); the normative spec is
`openspec/specs/contribution-simplicity/spec.md`. Reviewers apply them to
every PR (the budget checks are in `.github/scripts/check_simplicity_budgets.py`;
the `simplicity-budgets` workflow that runs it is disabled in this fork):

1. **New features default to off or zero-config.** No new required
   setup step (env var, migration action, external account, manual
   file edit) on the base install path without maintainer approval via
   the `simplicity-budget-approved` label.
2. **Every new `CODEX_LB_*` setting justifies not being a default.**
   The PR body answers "why can't this be a hardcoded default?" for
   each new setting; internals-only knobs stay out of `.env.example`.
3. **README, `.env.example`, and dashboard nav are budgeted.** The
   caps live in `.github/simplicity-budgets.toml`. Exceeding a cap
   requires the maintainer-applied `simplicity-budget-approved` label
   before merge.
4. **Feature docs go to `docs/` + OpenSpec, never new README
   sections.** Each spec-governed docs page links back to its
   `openspec/specs/<capability>/` entry.
5. **Dashboard-visible PRs include before/after screenshots** (or a
   short recording) in the PR body.
6. **The dashboard is the primary configuration surface** (PRINCIPLES.md
   P6). Every new setting names its tier (T0 bootstrap / T1 instance
   topology / T2 secret / T3 behaviour tunable / T4 incident debug) in the
   PR body; a T3 setting has a `dashboard_settings` column, not an
   env-only home; precedence stays code default < env < dashboard with no
   env-wins paths. Machine-checked by `scripts/check_settings_tiers.py`
   under `make lint`; the normative spec is linked from
   [PRINCIPLES.md P6](../PRINCIPLES.md#p6--the-dashboard-is-the-primary-configuration-surface).

### Collaborator rules

Collaborators (write-access contributors) follow two additional rules on
top of the merge gates above:

1. **No self-merge by default.** A collaborator's own PR is merged by
   another maintainer (or, until the project has more collaborators,
   by the project owner). Review independence matters more than
   turnaround.
2. **Large PRs get split.** Roughly:
   - If a PR is a stack tip pulling in unrelated commits from sibling
     branches, split it so each merged PR is a single scoped change.
   - If a single PR is over **~800 net lines** and spans multiple
     concerns, split it into reviewable pieces. A single 1500-line
     change scoped to one capability is fine; a 400-line change that
     touches the proxy hot path *and* the dashboard *and* the OAuth
     flow is not.

### Bus factor escape hatch

To keep the project unblocked if the owner is unavailable, the following
self-merge escape hatch applies:

- If a collaborator's PR has been waiting on a maintainer merge for
  **more than 14 days** with **all merge gates met** (CI green,
  CodeRabbit findings addressed, `mergeStateStatus=CLEAN`, no
  outstanding requested-changes review, no objection from any other
  active collaborator in the thread), the PR author may self-merge.
- Self-merge under this clause **must** include a comment on the PR
  explicitly invoking the clause and linking to the date the merge
  gates first went green. Audit trail must stay clean.
- The clause is a safety valve, not a default path. If you're tempted to
  invoke it on a PR you opened less than two weeks ago, the merge gates
  probably aren't actually all green yet.

### What this is not

These rules are intentionally lightweight. They don't require:

- A second human reviewer in addition to CodeRabbit for every PR.
  CodeRabbit review + the PR author + a maintainer merge is the baseline.
- Commit message work from the PR author beyond the Conventional Commits
  title. The maintainer who lands the change writes the body and adds the
  fork's trailers.
- A formal escalation process for disagreements. If a P1 finding is
  disputed, work it out in-thread; if it can't be resolved, leave the
  PR open and ping the owner.

## Tests

- **Unit tests** live under `tests/unit/` and should be fast and hermetic.
- **Integration tests** live under `tests/integration/` and may spin up the
  app, hit local sockets, or use fixtures that mimic upstream.
- New behavior needs at least one unit test. New endpoints need an
  integration test.
- Run a focused subset with:

```bash
uv run pytest tests/unit/test_proxy_api_responses_contract.py -q
```

## Release process

The fork's maintainer cuts releases as described in
[FORK.md](../FORK.md#releases-and-images): a version tag on `main`, container
images built by GitHub Actions, and a GitHub Release with notes. The fork
publishes no PyPI package or Helm chart. Upstream's release-please flow and
beta channel ([`openspec/specs/release-management/`](../openspec/specs/release-management/))
apply to upstream's releases, not to the fork's.

Contributors **never** need to edit `CHANGELOG.md`, version strings, or tag
manually.

Before releasing a train that includes migrations, inspect the Alembic
revisions directly (`git diff vPREV..HEAD -- app/db/alembic/versions`) for
data backfills and estimate their duration against a production-sized
dataset — they run at startup and block serving until they finish. Changelog
titles do not reveal backfills.

## Security issues

Please do **not** open public issues for security vulnerabilities. Report them
privately via
[GitHub Security Advisories](https://github.com/insciqq/codex-lb/security/advisories/new).
See [SECURITY.md](./SECURITY.md) for full details.

---

Happy hacking. If you get stuck, open a draft PR or start a Discussion —
maintainers would rather see a half-finished idea than no idea at all.
