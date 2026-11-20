## Why

The dashboard catch-all answered every unknown GET under `/internal/` with
`index.html` and a 200, so a probe for an operator or replica endpoint that
does not exist looked like it succeeded. Observed in production.

## What Changes

- Add `internal/` to the prefixes the catch-all refuses, next to `api/`, `v1/`,
  `backend-api/`, `health` and `scim/`.

## Capabilities

### Modified Capabilities

- frontend-architecture: the dashboard fallback is not served for backend
  path prefixes.

## Impact

`app/main.py` only. No dashboard route lives under `/internal/`.
