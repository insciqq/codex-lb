## Why

Opening a dashboard invite sends `GET /invite/<token>` before the page calls
`GET /api/dashboard-auth/invite/<token>`, and both precede redemption. The
access-log redactor masked only the API path, so the browser request wrote a
still-redeemable invite credential to the access log in both the text and the
JSON format. Access lines are INFO, below the level at which keyed secret
patterns run, so a credential passed as a query value (`?api_key=`, `?key=`,
`?access_token=`) was logged verbatim as well.

## What Changes

- Invite tokens are masked after any `/invite/` path segment, so the browser
  route and the API route are both covered; `/invite/accept` stays readable.
- Both access formatters mask the values of credential-bearing query
  parameters at every level, matching decoded parameter names; other
  parameters stay readable.

## Impact

Log output only. No setting, schema or API change.
