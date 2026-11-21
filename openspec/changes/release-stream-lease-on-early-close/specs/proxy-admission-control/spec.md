## ADDED Requirements

### Requirement: Native give-up closes the upstream stream and releases its account lease

When the native Responses path gives up on an upstream transport failure, the proxy MUST close the upstream stream generator that holds the account stream lease before it raises the `stream_incomplete` error. Each lease-holding wrapper between the consumer and the upstream stream MUST close its inner stream when it is closed or raises, so the upstream generator's cleanup runs and the account stream lease is released.

#### Scenario: Native transport give-up releases the stream lease

- **GIVEN** a native Responses stream whose upstream emitted at least one event and then a synthetic transport failure
- **WHEN** the proxy gives up and raises `stream_incomplete`
- **THEN** the upstream stream generator's cleanup runs
- **AND** the account stream lease is released

#### Scenario: Startup-probe and bridge-bypass wrappers release the lease before the raise

- **GIVEN** the service stream reaches the consumer through the route's startup probe (holding the first event it read or handing off its pending first read) or through the bridge-bypass relay of the raw-HTTP retry stream
- **WHEN** the native give-up raises `stream_incomplete` after an upstream event
- **THEN** every wrapper closes its inner stream before the error reaches the consumer
- **AND** the account stream lease is already released when the consumer sees the error

#### Scenario: Client leaves at the initial heartbeat after the probe read the first event

- **GIVEN** the route's startup probe already read the service stream's first event
- **AND** the body wrappers above the service stream have not started
- **WHEN** the client leaves after the initial heartbeat
- **THEN** the outer teardown closes the service stream and the account stream lease is released before the close returns

#### Scenario: Every relay of a lease-holding stream closes it on an early close

- **GIVEN** a lease-holding stream is relayed by the enabled HTTP bridge, the proxy-error relay, the chat-completions chunk relay, the cursor usage fallback, the images replay, the Images streaming translator or the Images non-streaming collector
- **WHEN** its consumer closes the relay early
- **THEN** the relay closes its inner stream before the close returns

#### Scenario: Client disconnects while a chunk is being sent

- **GIVEN** a streamed Responses or Images response whose service stream holds the account stream lease
- **WHEN** the client disconnects while the server is sending a chunk, or before the body started
- **THEN** the response closes its body and runs the stream teardown before the ASGI call returns
- **AND** the account stream lease is released

#### Scenario: The Images translator finishes its terminal event

- **GIVEN** an Images stream translated from a lease-holding Responses stream
- **WHEN** the translator emits its terminal event, or the collector stops at an early error
- **THEN** it closes the upstream stream

#### Scenario: Images client leaves before the body starts

- **GIVEN** an Images streaming route that primed its upstream and owns the image reservation
- **WHEN** the client disconnects while the response headers are being sent
- **THEN** the response closes the primed upstream and the service stream it wraps
- **AND** settles the image reservation exactly once

#### Scenario: The stream teardown outlives a cancelled caller

- **GIVEN** a Responses stream teardown that is waiting for the startup probe to unwind
- **WHEN** the task running the teardown is cancelled directly
- **THEN** the teardown still finishes its ownership check and reservation release
- **AND** a later close waits for the same teardown instead of skipping it
