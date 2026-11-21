## ADDED Requirements

### Requirement: Direct HTTP Responses dispatch streams the body to the native helper

The direct HTTP Responses path MUST serialize the request body at dispatch and
MUST NOT hold the serialized text for the rest of the attempt unless payload
tracing is enabled. The native egress request command MUST carry the same
body base64-encoded in its `body` field, written into the command line in
bounded slices without materializing the whole encoding as a string, a JSON
dump or a joined copy, and with pipe backpressure applied between slices. A
sender cancelled part way MUST queue the rest of the line before the
cancellation propagates, so the helper never receives a partial command.
Closing the client MUST close the helper's input only after the command being
written is complete, MUST NOT write commands still waiting to be sent, and
MUST terminate a helper that stops reading, or whose input does not flush,
after a bounded wait. Once a close has begun, cancelling it MUST NOT stop the
teardown: the helper and its reader MUST still be stopped before the
cancellation propagates, and no error may surface in the event loop when the
helper's input closes after the wait.

#### Scenario: Large body reaches the helper within a bounded peak

- **GIVEN** a Responses request of about 50 MB that exceeds the websocket frame budget
- **WHEN** it is dispatched to upstream HTTP through the native egress helper
- **THEN** the helper receives one command line carrying the whole body
- **AND** the dispatch allocates less than two and a half times the body size

#### Scenario: Cancelled sender completes the line

- **WHEN** the sender of a body command is cancelled while the pipe applies backpressure
- **THEN** the cancellation propagates and the helper still receives one complete command line

#### Scenario: Closing the client during a body command

- **GIVEN** a body command is being written to a helper that reads slowly, and another command waits to be sent
- **WHEN** the client is closed
- **THEN** the helper reads the body command whole and then end of input, and the waiting command is not written

#### Scenario: Closing the client when the helper stopped reading

- **WHEN** the client is closed while the helper no longer reads its input, whether a command holds the write lock or unflushed input remains
- **THEN** the close completes within a bounded time, the helper is terminated, and the event loop reports no error

#### Scenario: Cancelled close

- **WHEN** a close is cancelled while a body command is still being written
- **THEN** the command completes, the helper exits and its reader stops, and the close then raises the cancellation

#### Scenario: Command line decodes to the original body

- **WHEN** a request body of any length, including lengths that cross slice boundaries, is written to the helper
- **THEN** the command line is one JSON object whose `body` decodes to the original bytes
