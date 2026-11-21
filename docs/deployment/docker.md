# Docker

## Basic run

```bash
docker volume create codex-lb-data
docker network inspect codex-lb-net >/dev/null 2>&1 || docker network create codex-lb-net
docker run -d --name codex-lb \
  --network codex-lb-net \
  -p 2455:2455 -p 1455:1455 \
  -v codex-lb-data:/var/lib/codex-lb \
  ghcr.io/insciqq/codex-lb:1.25.0-fork.1
```

Ports:

- `2455` — dashboard + proxy API
- `1455` — OAuth login callback (needed while adding accounts)

The volume holds everything under `/var/lib/codex-lb/` (database, encryption key, archives) — back it up to preserve your data.

## Images

The fork publishes each release as two tags:

- `ghcr.io/insciqq/codex-lb:<version>`, for example `1.25.0-fork.1`
- `ghcr.io/insciqq/codex-lb:sha-<full 40-character commit SHA>`

There is no `latest` or other floating tag. For production, pin the `sha-` tag
or the image digest, and verify the digest's build provenance first:

```bash
gh attestation verify oci://ghcr.io/insciqq/codex-lb@<digest> \
  --repo insciqq/codex-lb --source-digest <full commit SHA> \
  --signer-workflow insciqq/codex-lb/.github/workflows/fork-image.yml
```

The release policy is in [FORK.md](https://github.com/insciqq/codex-lb/blob/main/FORK.md#releases-and-images).

## Switching Wi-Fi or other networks

When a laptop switches from one Wi-Fi network to another—for example, from home Wi-Fi to a phone hotspot—or when a VPN connects or disconnects, existing internet connections may briefly break. Docker can also keep using a DNS server from the previous network. DNS is the service that finds the network address for names such as `chatgpt.com`; if Docker's copy is out of date, codex-lb may report timeouts while contacting OpenAI even though the host browser works.

codex-lb retries only when the transport can prove that the request failed before it was sent. Merely seeing no output is not enough: if a request may already have reached OpenAI, codex-lb returns the network error without resending it, which avoids accidentally starting the same response twice. In either case, it avoids treating a laptop-wide DNS problem as a problem with an individual account. It cannot, however, repair a Docker DNS service that remains pointed at the old network.

For laptops that switch networks frequently:

- **Simplest on Linux, macOS, and Windows:** run `uvx codex-lb` (upstream's PyPI release; the fork publishes no Python package) directly on the host. This avoids Docker's additional DNS layer.
- **Docker Engine on Linux (verified with `systemd-resolved`):** use host networking so the container shares the host resolver path. This survives network switches only when the host exposes a stable resolver address, such as the `127.0.0.53` `systemd-resolved` stub. If the host's `/etc/resolv.conf` points directly to a DNS server supplied by Wi-Fi or other DHCP, that address can still become stale. In that case, configure a stable host resolver, follow the [bridge-listener runbook](https://github.com/insciqq/codex-lb/blob/main/openspec/specs/deployment-networking/context.md#diagnostics-and-recovery), or prefer `uvx`. Use the following command instead of the portable Docker command above.
- **Docker Desktop on macOS or Windows:** Docker Desktop 4.34 and later offers opt-in host networking, but containers still run through Docker Desktop's virtual machine and its DNS behavior can vary by version and configuration. This setup has not been verified as a reliable fix for switching networks. Keep Docker Desktop current; if failures persist, prefer the native `uvx` installation.

```bash
docker volume create codex-lb-data
docker run -d --name codex-lb \
  --network host \
  -v codex-lb-data:/var/lib/codex-lb \
  ghcr.io/insciqq/codex-lb:1.25.0-fork.1
```

In the verified Docker Engine setup on Linux, host networking does not use `-p`; codex-lb still listens on ports 2455 and 1455. It also removes Docker's network-namespace isolation. The command is an opt-in path to a stable host resolver, not a DNS fix by itself.

## Docker Compose

For a production-shaped Compose setup (watchtower-friendly tags, external PostgreSQL via env), start from
[`docker-compose.prod.yml`](https://github.com/insciqq/codex-lb/blob/main/docker-compose.prod.yml) — it defines
only the `server` service. The optional `postgres` / `postgres-upgrade` profiles live in the root
[`docker-compose.yml`](https://github.com/insciqq/codex-lb/blob/main/docker-compose.yml) (see [Database](../database.md)):

```bash
cp .env.example .env.local   # required: the compose file references .env.local via env_file — an unedited copy still runs with zero config
docker compose -f docker-compose.prod.yml up -d
```

For PostgreSQL profiles and the Postgres 16 → 18 upgrade runbook, see [Database](../database.md).

## Graceful shutdown

On `SIGTERM` codex-lb stops admitting new work, drains in-flight requests for
up to `CODEX_LB_SHUTDOWN_DRAIN_TIMEOUT_SECONDS` (30 seconds by default) and then
runs up to 25 seconds of bounded cleanup (bridge sessions, persistence, database
close). Docker kills a container 10 seconds after `docker stop` unless told
otherwise, which cuts that drain short while a stream is in flight. Give the
container at least the drain timeout plus 30 seconds (the 25-second cleanup
reserve and a 5-second margin), which is 60 seconds at the default: the bundled
Compose files set `stop_grace_period: 65s`, matching the Helm chart's
`terminationGracePeriodSeconds`. With plain `docker run`, add
`--stop-timeout 65`, or stop it with `docker stop -t 65 codex-lb`. Raise the
value together with the drain timeout. Contract:
[deployment-installation](https://github.com/insciqq/codex-lb/tree/main/openspec/specs/deployment-installation).

## Auth mode examples

**Authelia / trusted header**

```bash
docker run -d --name codex-lb \
  -p 2455:2455 -p 1455:1455 \
  -e CODEX_LB_DASHBOARD_AUTH_MODE=trusted_header \
  -e CODEX_LB_DASHBOARD_AUTH_PROXY_HEADER=Remote-User \
  -e CODEX_LB_FIREWALL_TRUST_PROXY_HEADERS=true \
  -e CODEX_LB_FIREWALL_TRUSTED_PROXY_CIDRS=172.18.0.0/16 \
  -v codex-lb-data:/var/lib/codex-lb \
  ghcr.io/insciqq/codex-lb:1.25.0-fork.1
```

**Hard override / no app-level dashboard auth**

```bash
docker run -d --name codex-lb \
  -p 2455:2455 -p 1455:1455 \
  -e CODEX_LB_DASHBOARD_AUTH_MODE=disabled \
  -v codex-lb-data:/var/lib/codex-lb \
  ghcr.io/insciqq/codex-lb:1.25.0-fork.1
```

For Helm, pass the same values through `extraEnv`. What these modes mean and when to use them is covered in [Authentication](../authentication.md).

---

*Specs: [deployment-installation](https://github.com/insciqq/codex-lb/tree/main/openspec/specs/deployment-installation) · [deployment-networking](https://github.com/insciqq/codex-lb/tree/main/openspec/specs/deployment-networking)*
