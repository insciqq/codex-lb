Regression reproduced: a code-less HTTP 400 `Stream must be set to true` failed
over to every account and recorded a transient account error on each; through
the compact and post-refresh paths it also failed over. A code-less
`Unsupported parameter` 400 still fails over to an account that accepts the
parameter.

Validation after the fix:
- `tests/integration/test_proxy_transient_retry.py` and
  `tests/unit/test_failover_foundation.py` passed.
- Ruff lint and formatting checks passed.
- Full `ty check` passed.
- Strict change validation passed.
