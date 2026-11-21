## 1. Implementation

- [x] 1.1 Build both retry-circuit suppression refusals through one helper that flags them as local pre-dispatch refusals.

- [x] 1.2 Carry the owner's `Retry-After` over the internal bridge forward into the origin's error.

## 2. Verification

- [x] 2.1 `tests/integration/test_http_responses_bridge.py::test_native_codex_http_bridge_retry_circuit_suppression_returns_503_before_commit`
  fails without the flag (empty `200`) and passes with it.
- [x] 2.2 `tests/integration/test_http_responses_bridge.py::test_native_codex_http_bridge_retry_circuit_suppression_emits_terminal_after_commit`
  fails without the flag (empty committed body) and passes with it.
- [x] 2.3 `test_native_codex_owner_forward_retry_circuit_refusal_keeps_retry_after_before_commit` and
  `test_native_codex_owner_forward_retry_circuit_refusal_keeps_retry_hint_after_commit` fail without 1.2.
