## Why

The request decompression middleware drains an encoded body before it
decompresses it. When the client went away mid-body, the drain raised
starlette's `ClientDisconnect`, which escaped the ASGI app and was logged as an
application error. Observed in production.

## What Changes

- End the request quietly on a client disconnect while draining: the route is
  not called and nothing is sent.
- Log the aborted upload at debug level with method and path.
- Other body read failures still propagate.

## Capabilities

### Modified Capabilities

- http-ingress-limits: a client disconnect while an encoded body is read ends
  the request without an error.

## Impact

`app/core/middleware/request_decompression.py` only. No setting, no
client-visible change: the client has already gone.
