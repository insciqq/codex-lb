## Why

Upstream answers some requests with an HTTP 400 whose whole message names a
field of the request, for example `Stream must be set to true`. The body often
carries no error code, so it normalizes to `upstream_error`, which the failure
classifier treats as transient. Observed in production: one such request was
failed over to two more accounts and recorded a transient account error on
each of the three, although every account rejects it identically. A client
that keeps sending the request pushes healthy accounts into error backoff,
and the failover walk moves sticky sessions off their owners for nothing.

## What Changes

- Add one predicate for request-shape rejections: HTTP 400 (or a status no
  longer known at the health write), code `upstream_error` or
  `invalid_request_error`, and the entire message `Stream must be set to
  true`. Only sentences proven independent of account, plan and model belong
  in the list; parameter rejections such as `Unsupported parameter: <name>`
  can differ between accounts and keep failing over.
- Classify a match `non_retryable`, before the transient branch, so it is
  surfaced from the first account without failover.
- Treat a match as an account-neutral request rejection, so no health write
  happens on the surface path either.
- Quota, rate-limit, auth, overload, model-capacity, model-entitlement and
  every coded failure keep their current classification and health writes.

## Capabilities

### Modified Capabilities

- account-routing: request-shape rejections are non-retryable and account
  neutral.

## Impact

`app/modules/proxy/helpers.py` (predicate and one classifier branch) and
`app/modules/proxy/_service/streaming/helpers.py` (account-neutral check). No
setting, no schema change, no change to the error returned to the client.
Routing of valid requests and sticky/prompt-cache affinity are unchanged: the
change only removes failover attempts and account exclusions for requests no
account can serve.
