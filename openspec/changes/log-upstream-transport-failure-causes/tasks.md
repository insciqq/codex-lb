## 1. Implementation

- [x] 1.1 Add an optional `detail` to the helper's HTTP `error` event rendered from typed fields of h2, I/O, TLS and hyper causes, bounded to 512 bytes.
- [x] 1.2 Keep the detail on `NativeEgressTransportError` and log it with the failure phase and request id.
- [x] 1.3 Log the close code of an upstream websocket close before a terminal event; warn only for a peer or lost-connection close; log the reason only from a fixed set of generic phrases.

## 2. Verification

- [x] 2.1 `crates/codex-lb-egress/src/http.rs` unit tests: typed rendering drops free text, the bound, and a GOAWAY carrying a signed URL in its debug data.
- [x] 2.2 `tests/unit/test_native_egress.py::test_subprocess_native_egress_logs_the_helper_failure_detail`.
- [x] 2.3 `tests/unit/test_proxy_utils.py` close-log tests: code and generic reason, free-text reasons with signed-URL and request-content sentinels, and a local close.
