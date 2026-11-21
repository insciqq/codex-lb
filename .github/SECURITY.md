# Security Policy

This repository is a maintained fork of
[Soju06/codex-lb](https://github.com/Soju06/codex-lb). This policy covers the
fork: its `main` branch, its releases and its container images. See
[FORK.md](../FORK.md) for how the fork is built and released.

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

Instead, report them privately using GitHub's
[Private Vulnerability Reporting](https://github.com/insciqq/codex-lb/security/advisories/new):

1. Open <https://github.com/insciqq/codex-lb/security/advisories/new>
2. Fill in a clear title and a detailed description of the issue.
3. Include reproduction steps, affected versions, and any proof-of-concept you
   have.
4. Submit. The report is visible only to repository maintainers.

If for some reason you cannot use the private advisory flow, open a
[Discussion](https://github.com/insciqq/codex-lb/discussions) titled
`[security] request for private contact` that contains **no** vulnerability
details, and a maintainer will arrange a private channel.

## Upstream

Most of the code comes from upstream Soju06/codex-lb. When a reported
vulnerability also affects upstream, the fork maintainers report it to
upstream privately where appropriate, after agreeing on it with you. You may
also report it to upstream yourself through
[upstream's security policy](https://github.com/Soju06/codex-lb/security/policy).

## What to include

A good report makes triage much faster:

- **Affected version(s)**: the fork release tag (`v<upstream base>-fork.<n>` or
  `v<major>.<minor>.<patch>`), the image digest, or the commit SHA.
- **Deployment**: Docker image / Helm chart from a release tag / from source.
- **Impact**: what can an attacker do? (data disclosure, account takeover,
  RCE, DoS, auth bypass, log injection, etc.)
- **Reproduction**: minimal steps — config snippet, request payload, log
  excerpt. Redact your own secrets before sending.
- **Suggested fix** (optional): if you already have a patch in mind.

## Scope

In scope:

- The codex-lb proxy (`app/`) — auth, routing, account management, the
  dashboard backend, the `/v1/*` and `/backend-api/*` surfaces.
- The dashboard frontend (`frontend/`).
- The fork's container images (`ghcr.io/insciqq/codex-lb`).
- The Helm chart in `deploy/helm/codex-lb/` as shipped at a fork release tag.
  The fork does not publish a Helm chart to a registry; it is installed from a
  checkout of the release tag (see the
  [chart README](../deploy/helm/codex-lb/README.md#installing-from-this-fork)).

Out of scope (please don't file these as security advisories here):

- Artifacts the fork does not publish: upstream's PyPI package, Docker image
  and Helm chart. Report those to upstream.
- Issues in upstream services (ChatGPT, OpenAI Codex, model providers).
- Vulnerabilities in third-party dependencies that don't reach codex-lb's
  attack surface — file those with the dependency.
- Self-inflicted misconfiguration (exposing the dashboard publicly without
  auth, leaking your own API keys via committed `.env`, etc.).
- Findings on releases older than the latest fork release, unless they affect
  the latest release too.

## Disclosure process

Fixes are handled on a best-effort basis; the fork makes no fixed response or
release deadlines.

1. **Acknowledge** — a maintainer confirms receipt.
2. **Triage & validate** — the maintainer confirms the issue, asks for more
   information, or declines with reasoning.
3. **Fix & coordinate** — for confirmed issues, the maintainer drafts a fix
   privately; severe issues are prioritized.
4. **Release & advisory** — once a fork release with the fix is published, the
   maintainer publishes a GitHub Security Advisory (with a CVE if applicable)
   crediting you, unless you request anonymity.
5. **Embargo** — please do not publicly disclose the issue until the advisory
   is published. If a fix is delayed, we will agree on a coordinated
   disclosure date with you.

## Supported versions

Only the **latest fork release** receives security fixes. Fork releases are
tagged `v<upstream base>-fork.<n>`, or plain `v<major>.<minor>.<patch>` for a
release of the fork's own, on the fork's `main` branch; see
[Releases](https://github.com/insciqq/codex-lb/releases). Older releases do
not receive backports — please upgrade.

Each release publishes `ghcr.io/insciqq/codex-lb:<version>` and
`ghcr.io/insciqq/codex-lb:sha-<full commit SHA>`, and `latest` follows the
newest release. Deploy by the `sha-` tag or by digest and verify the digest's
build provenance first ([FORK.md](../FORK.md#releases-and-images)).

## Hall of fame

We're happy to credit reporters in the published advisory and (with permission)
in this section. Researchers who responsibly disclose a confirmed vulnerability
will be acknowledged here.

_(none yet — be the first.)_
