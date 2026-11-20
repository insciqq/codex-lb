## 1. Classification

- [x] 1.1 Add `is_upstream_request_shape_rejection` to `app/modules/proxy/helpers.py`
- [x] 1.2 Classify a match `non_retryable` in `classify_upstream_failure`

## 2. Account health

- [x] 2.1 Treat a match as an account-neutral request rejection in `_handle_stream_error`

## 3. Tests

- [x] 3.1 Routed regression: a request-shape 400 hits one account and moves no account's health
- [x] 3.2 Classifier table: status, code and whole-message guards
- [x] 3.3 Routed regression: a code-less `Unsupported parameter` 400 still fails over to an account that accepts the request
