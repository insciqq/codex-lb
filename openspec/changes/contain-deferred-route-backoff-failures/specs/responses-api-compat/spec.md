## ADDED Requirements

### Requirement: Deferred route backoff failures preserve stream completion

After the API-key reservation of a stream has settled or been released, the
proxy MUST log an ordinary failure of a deferred route-backoff write instead
of raising it. It MUST NOT settle again or emit another terminal event, MUST
keep the failed account queued for the request's final post-settlement drain,
and MUST let cancellation propagate.

#### Scenario: Backoff write fails after the replacement completed

- **GIVEN** a keyed stream whose first account failed before dispatch and whose replacement emitted `response.completed`
- **WHEN** the deferred route-backoff write for the first account raises after settlement
- **THEN** the client receives exactly one terminal event
- **AND** the failure is logged and the backoff is retried once by the final cleanup

#### Scenario: Cancellation during post-settlement cleanup

- **WHEN** the stream is cancelled and a deferred route-backoff write then fails
- **THEN** the cancellation propagates and the backoff failure is logged
