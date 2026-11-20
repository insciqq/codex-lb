## ADDED Requirements

### Requirement: Dashboard fallback is not served for backend paths

The dashboard catch-all MUST NOT answer a GET under a backend-only path prefix with `index.html`. An unknown path under `/api/`, `/v1/`, `/backend-api/`, `/health`, `/scim/` or `/internal/`, or equal to one of those prefixes, MUST return 404 so a probe for a backend endpoint that does not exist is never told it succeeded. Paths outside those prefixes that do not name a static asset MUST keep receiving `index.html` so dashboard routes load.

#### Scenario: Unknown internal path is a 404

- **GIVEN** the dashboard build is installed
- **WHEN** a client sends `GET /internal/unknown`
- **THEN** the response is 404 and not `index.html`

#### Scenario: Dashboard routes still fall back to the SPA

- **GIVEN** the dashboard build is installed
- **WHEN** a client sends `GET /dashboard/settings`
- **THEN** the response is `index.html` with 200
