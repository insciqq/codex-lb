Regression reproduced: `GET /internal`, `/internal/unknown` and
`/internal/drain/unknown` returned `index.html` with 200.

Validation after the fix:
- `tests/integration/test_health_and_errors.py` passed (the poisoned-registry
  asset test needs a frontend build and is unrelated).
- Ruff lint and formatting checks passed.
- Strict change validation passed.
