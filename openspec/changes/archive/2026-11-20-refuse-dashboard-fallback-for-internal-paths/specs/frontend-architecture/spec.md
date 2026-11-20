## ADDED Requirements

### Requirement: Dashboard fallback is not served for backend paths

The dashboard catch-all MUST NOT answer a GET under a backend-only path prefix with `index.html`. An unknown path under `/api/`, `/v1/`, `/backend-api/`, `/health`, `/scim/` or `/internal/`, or equal to one of those prefixes, MUST return 404 so a probe for a backend endpoint that does not exist is never told it succeeded. A path that cannot name a file — one with an embedded NUL, a segment longer than the filesystem name limit, or a total length above the filesystem path limit — MUST return 404 as well. Any other path outside those prefixes that does not name a static asset MUST keep receiving `index.html` so dashboard routes load; an error looking up a static asset other than those MUST NOT be turned into a 404.

#### Scenario: Unknown internal path is a 404

- **GIVEN** the dashboard build is installed
- **WHEN** a client sends `GET /internal/unknown`
- **THEN** the response is 404 and not `index.html`

#### Scenario: Path that cannot name a file is a 404

- **GIVEN** the dashboard build is installed
- **WHEN** a client sends a GET whose path has an embedded NUL or a segment longer than the filesystem name limit
- **THEN** the response is 404 and not `index.html`

#### Scenario: Dashboard routes still fall back to the SPA

- **GIVEN** the dashboard build is installed
- **WHEN** a client sends `GET /dashboard/settings`
- **THEN** the response is `index.html` with 200
