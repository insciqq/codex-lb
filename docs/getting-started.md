# Getting Started

codex-lb runs with zero configuration — every setting has a working default, and Docker vs. host paths are auto-detected.

## Quick Start

```bash
# Docker (recommended)
docker volume create codex-lb-data
docker run -d --name codex-lb \
  --stop-timeout 65 \
  -p 2455:2455 -p 1455:1455 \
  -v codex-lb-data:/var/lib/codex-lb \
  ghcr.io/insciqq/codex-lb:1.25.0-fork.1

# or Nix
nix run github:insciqq/codex-lb/v1.25.0-fork.1
```

`uvx codex-lb` installs upstream's PyPI release, not this fork: the fork publishes container images only.

The image tags are `<version>` and `sha-<full commit SHA>`; there is no `latest`.
Pin the `sha-` tag or the digest in production, see [Docker](deployment/docker.md#images).

Open [localhost:2455](http://localhost:2455) → Add account → Done.

Next: point your coding agent at codex-lb — see [Client Setup](client-setup.md).

## Remote setup (bootstrap token)

When accessing the dashboard remotely for the first time, a bootstrap token is required to set the initial password.

**Auto-generated (default):** On first startup (no password configured), the server generates a one-time token and prints it to logs:

```bash
docker logs codex-lb
# ============================================
#   Dashboard bootstrap token (first-run):
#   <token>
# ============================================
```

Open the dashboard → enter the token + new password → done. The token is shared across replicas and remains valid until a password is set. In multi-replica setups, replicas must share the same encryption key (the Helm chart default) for restart recovery to work — see [Kubernetes deployment](deployment/kubernetes.md).

**Manual token:** To use a fixed token instead, set the env var before starting:

```bash
docker run -d --name codex-lb \
  -e CODEX_LB_DASHBOARD_BOOTSTRAP_TOKEN=your-secret-token \
  -p 2455:2455 -p 1455:1455 \
  -v codex-lb-data:/var/lib/codex-lb \
  ghcr.io/insciqq/codex-lb:1.25.0-fork.1
```

**Local access** (localhost) bypasses bootstrap entirely — no token needed.

Running behind a reverse proxy or exposing codex-lb to other machines? See [Remote Access](deployment/remote.md) and [Authentication](authentication.md).

---

*Spec: [deployment-installation](https://github.com/insciqq/codex-lb/tree/main/openspec/specs/deployment-installation)*
