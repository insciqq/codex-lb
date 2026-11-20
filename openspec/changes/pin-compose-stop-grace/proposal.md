## Why

On `SIGTERM` codex-lb drains in-flight requests for the shutdown drain timeout
and then runs up to 25 seconds of bounded cleanup. The bundled Compose files set
no `stop_grace_period`, so Docker's default 10-second grace killed the process
mid-drain whenever a stream was in flight. The Helm chart already pins its
termination grace in this capability; Compose had no recorded minimum.

## What Changes

- Record the Compose minimum: drain timeout + 25-second cleanup reserve + 5
  seconds (60 seconds at the default). The bundled files ship 65 seconds.
- The Docker deployment guide and the contract test use the same bound.

## Impact

- Affected capability: `deployment-installation`.
- Affected files: `docker-compose.yml`, `docker-compose.prod.yml`,
  `docs/deployment/docker.md`.
