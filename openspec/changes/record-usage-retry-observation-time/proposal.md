## Why

Usage refresh stamps the persisted sample with the time the fetch started, so
a live sample recorded while the poll was in flight cannot supersede it. When
the first fetch failed with 401 and succeeded after a credential refresh, the
retried sample reused the failed attempt's timestamp. A live 100% sample
recorded during the credential refresh then stayed the latest row: the account
became active in the database while the live balancer kept refusing it.

## What Changes

- Capture the observation time immediately before each usage fetch dispatch
  and persist the time of the attempt that supplied the payload.
- Live samples keep their enqueue-time timestamps.

## Impact

- Affected capability: `usage-refresh-policy`.
- Affected code: `app/modules/usage/updater.py`.
