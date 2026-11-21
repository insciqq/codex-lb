## Why

When the native egress helper's upstream HTTP request failed, the helper
reported only a static message such as "native upstream response body failed"
and dropped the hyper, h2, TLS and I/O cause, and its stderr is discarded. When
the per-request upstream websocket closed before a terminal event, the stream
readers stopped at the close without recording its code. In both cases the
logs held no evidence of why the upstream stream died (an h2 reset reason, a
TLS alert, a dropped connection with close code 1006).

## What Changes

- The native helper's HTTP `error` event carries an optional `detail` built
  only from typed fields of known cause types: the h2 error kind, origin and
  reason code, the I/O error kind and OS error number, the TLS alert, and
  hyper's fixed error-kind description. Free-form display text is never
  copied: the reqwest layers embed the request URL, an h2 GOAWAY carries the
  peer's debug data, and I/O or TLS errors can wrap arbitrary text. The detail
  is bounded to 512 bytes. The Python client keeps it on the transport error
  and logs it with the failure phase and the request id. It is not added to the
  error message a client receives.
- The per-request upstream websocket readers log the close code and message
  type when the socket closes before a terminal event: at warning level for a
  peer close frame or a lost connection, at debug level after our own close.
  The peer's close reason is logged only when it is one of a fixed set of
  generic phrases; any other reason is logged as `<unrecognized>`.
- Connection pooling, retries, failover and error codes are unchanged.

## Impact

`crates/codex-lb-protocol`, `crates/codex-lb-egress` (one optional event
field, a direct dependency on the already locked `h2`),
`app/core/clients/native_egress.py`, `app/core/clients/proxy.py` and their
tests. No setting or schema change. A helper without the field keeps working;
the detail is then absent.
