Regression reproduced: a disconnect mid-body raised `ClientDisconnect` out of
the application.

Validation after the fix:
- `tests/unit/test_request_decompression_middleware.py`,
  `tests/integration/test_request_decompression.py` and the other ingress
  middleware tests passed.
- Ruff lint and formatting checks passed.
- Full `ty check` passed.
- Strict change validation passed.
