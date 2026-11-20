## Why

Starting the in-process Prometheus metrics server built its uvicorn config
with uvicorn's stock log config and `log_level="warning"`. uvicorn applies
both process-wide, so enabling metrics replaced the server's configured
handlers on the `uvicorn.*` loggers and silenced every access-log line. In
JSON log format, access records also carried `null` client, request and
status, because uvicorn derives those fields only on a copy of the record
inside its own access formatter.

## What Changes

- The metrics server leaves process logging untouched (`log_config=None`,
  `log_level=None`).
- The JSON access formatter derives client, request line and status from the
  uvicorn access record arguments when the derived attributes are absent; the
  request line keeps the existing redaction.

## Impact

No setting, schema or CLI change. Taken from upstream PR 2507 without its new
`--log-level` and `--log-file` flags.
