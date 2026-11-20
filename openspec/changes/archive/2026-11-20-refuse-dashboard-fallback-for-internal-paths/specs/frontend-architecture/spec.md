## ADDED Requirements

### Requirement: Dashboard fallback is not served for backend paths

The dashboard catch-all MUST NOT answer a GET under a backend-only path prefix with `index.html`. An unknown path under `/api/`, `/v1/`, `/backend-api/`, `/health`, `/scim/` or `/internal/`, or equal to one of those prefixes, MUST return 404 so a probe for a backend endpoint that does not exist is never told it succeeded. Outside those prefixes the response MUST follow the outcome of looking the path up under the static directory: a lookup rejected for an embedded NUL, or failing because a name or the whole path is too long for the filesystem (`ENAMETOOLONG`), MUST return 404. A lookup that finds no file — including an over-long name below a directory that does not exist, which the filesystem reports as missing before it checks the name length — keeps the ordinary behaviour: a path whose last segment has an extension or that is under `/assets/` returns 404, and any other path receives `index.html` so dashboard routes load. Any other lookup error MUST NOT be turned into a 404.

#### Scenario: Unknown internal path is a 404

- **GIVEN** the dashboard build is installed
- **WHEN** a client sends `GET /internal/unknown`
- **THEN** the response is 404 and not `index.html`

#### Scenario: Path the filesystem rejects is a 404

- **GIVEN** the dashboard build is installed
- **WHEN** a client sends a GET whose path has an embedded NUL, or whose lookup fails with `ENAMETOOLONG` (for example a 300-character top-level segment)
- **THEN** the response is 404 and not `index.html`

#### Scenario: Missing extensionless route keeps the SPA fallback

- **GIVEN** the dashboard build is installed and has no `dashboard/` directory
- **WHEN** a client sends a GET for `/dashboard/` followed by a 300-character segment
- **THEN** the lookup reports the file missing, and the response is `index.html` with 200

#### Scenario: Dashboard routes still fall back to the SPA

- **GIVEN** the dashboard build is installed
- **WHEN** a client sends `GET /dashboard/settings`
- **THEN** the response is `index.html` with 200
