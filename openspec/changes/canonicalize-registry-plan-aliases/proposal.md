## Why

Usage refresh canonicalizes the upstream plan `self_serve_business_prolite` to
`prolite`, but a model-registry snapshot persisted before that change can
still list the raw alias as an allowed plan. Model-plan eligibility compared
the canonical account plan against the uncanonicalized catalog plans, so a
`prolite` account was filtered out of model selection until the catalog
refreshed, and a sticky owner could become unavailable in the meantime.

## What Changes

- Canonicalize known plan aliases on the catalog side of model-plan
  eligibility as well as on the account side.

## Impact

- Affected capability: `usage-refresh-policy`.
- Affected code: `app/core/plan_types.py`.
- No migration, setting or API change.
