## Why

A `response.create` larger than the upstream limit, or one upstream closes with
1009, is saved as a debug dump of the whole request text. The dump hashed and
gzip-compressed the payload on the event loop. Near the upstream limit that is
seconds of CPU, and observed in production every other request on the instance
stalled for as long as each dump took.

## What Changes

- Write the dump in a worker thread from a snapshot of the request fields it
  records; the rejection is returned without waiting for it.
- Write dumps one at a time in request order, so fingerprint dedup also
  catches a retry that arrives while its dump is being written.
- Bound pending dumps at four and at 64 MiB of retained payload text; ingress
  admits bodies far above the upstream limit, so a count alone could pin
  hundreds of MiB. A dump beyond either bound, or a single payload above the
  budget, is dropped with a warning naming the request and its size; the drop
  path does no hashing or encoding on the loop.
- Skip the meta field summary above 1 MiB: it parses the whole payload, which
  holds the GIL and so stalls the loop even from a worker thread. Dumps of
  payloads over the upstream limit therefore never carry it; derive it offline
  from the `.gz` file.
- Graceful shutdown settles pending dumps last, after the API-key flush,
  leader release and database close, and only within what remains of the
  shared shutdown deadline minus a 1 s margin. At the cutoff each queued dump
  is stopped and logged with its request id, a warning gives their count, and
  a write still running is reported with its request id.
- Fingerprint dedup, retention, and the dump location are unchanged. No setting
  enables or disables dumps.

## Capabilities

### Modified Capabilities

- deployment-installation: the response-create dump contract covers background
  completion, the busy skip, and shutdown.

## Impact

`app/modules/proxy/_service/response_create.py` and the lifespan shutdown in
`app/main.py`. No setting, no schema change, no client-visible change.
