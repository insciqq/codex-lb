## ADDED Requirements

### Requirement: Compose stop grace covers the application drain

The bundled Compose files MUST set the `server` service's `stop_grace_period` to at least the shutdown drain timeout plus the 25-second post-drain cleanup reserve plus 5 seconds, which is 60 seconds at the default 30-second drain timeout; they ship 65 seconds, matching the Helm chart default. The Docker deployment guide MUST state that minimum, the `docker run --stop-timeout` and `docker stop -t` equivalents, and that the value rises with the drain timeout.

#### Scenario: Container stopped while a stream is in flight

- **GIVEN** the default 30-second drain timeout and a stream in flight
- **WHEN** an operator runs `docker compose stop` on a bundled Compose file
- **THEN** Docker waits at least 60 seconds before killing the process, so the drain and the bounded cleanup can finish
