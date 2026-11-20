## ADDED Requirements

### Requirement: Access logs mask invite tokens and credential query values

Access-log records in the text and JSON formats MUST mask the invite token in
both the dashboard route `/invite/<token>` and the API route
`/api/dashboard-auth/invite/<token>` at every level. They MUST mask, at every
level, the value of any query parameter whose decoded name denotes a
credential (for example `api_key`, `key`, `access_token`, `client_secret`),
while the route and other parameters stay readable.

#### Scenario: Browser invite request

- **WHEN** a client requests `GET /invite/<token>`
- **THEN** the access line shows `GET /invite/[REDACTED]` and never the token

#### Scenario: Credential query value at INFO

- **WHEN** a client requests `GET /v1/models?api_key=<secret>&model=gpt`
- **THEN** the access line shows `api_key=[REDACTED]&model=gpt`

#### Scenario: Ordinary parameters

- **WHEN** a client requests `GET /health?probe=1`
- **THEN** the access line shows the request unchanged
