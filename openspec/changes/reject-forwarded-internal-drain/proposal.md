## Why

Internal drain control authorizes the raw socket peer and allowed any loopback
peer, even one that relays a forwarded client address. A reverse proxy on the
same host connects over loopback, so every remote client it forwards gained
authority to start, stop and read drain on these unauthenticated endpoints.

## What Changes

- Reject drain requests that carry forwarding evidence, even from a loopback
  peer. Only direct local callers, such as the preStop helper, can drain.
- Document that a same-host relay adding no forwarding header (for example
  a TCP proxy or port forward) is indistinguishable from a direct caller, so
  same-host public proxies must not expose `/internal/drain/*`. Drain
  authentication itself is unchanged.
- The preStop helper sends its drain calls directly, ignoring `HTTP_PROXY`
  and similar settings in its environment, so a forward proxy cannot relay
  (and get rejected for) the hook's own calls.

## Impact

- Affected capability: `graceful-shutdown`.
- Affected code: `app/modules/health/api.py`.
- Operator-visible: drain calls must go directly to the process over loopback,
  not through a proxy. The bundled preStop helper already does this.
