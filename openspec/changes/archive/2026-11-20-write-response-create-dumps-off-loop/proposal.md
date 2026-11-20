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
- Graceful shutdown waits for pending dumps within the remaining drain deadline
  or post-drain reserve. A started write completes; each queued dump that is
  not written is logged with its request id, plus a warning with their count.
- Fingerprint dedup, retention, and the dump location are unchanged. No setting
  enables or disables dumps.

## Capabilities

### Modified Capabilities

- deployment-installation: the response-create dump contract covers background
  completion, the busy skip, and shutdown.

## Impact

`app/modules/proxy/_service/response_create.py` and the lifespan shutdown in
`app/main.py`. No setting, no schema change, no client-visible change.
