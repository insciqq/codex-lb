## 1. Implementation

- [x] 1.1 Build the metrics server config without a log config or log level.
- [x] 1.2 Derive JSON access fields from uvicorn access record arguments.

## 2. Verification

- [x] 2.1 Regression: building the metrics server config leaves the server logging configuration unchanged.
- [x] 2.2 Subprocess and unit regressions: JSON access lines carry client, request and status.
