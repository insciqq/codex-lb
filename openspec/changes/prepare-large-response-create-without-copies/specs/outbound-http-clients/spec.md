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

#### Scenario: Large body reaches the helper within a bounded peak

- **GIVEN** a Responses request of about 50 MB that exceeds the websocket frame budget
- **WHEN** it is dispatched to upstream HTTP through the native egress helper
- **THEN** the helper receives one command line carrying the whole body
- **AND** the dispatch allocates less than two and a half times the body size

#### Scenario: Cancelled sender completes the line

- **WHEN** the sender of a body command is cancelled while the pipe applies backpressure
- **THEN** the cancellation propagates and the helper still receives one complete command line

#### Scenario: Command line decodes to the original body

- **WHEN** a request body of any length, including lengths that cross slice boundaries, is written to the helper
- **THEN** the command line is one JSON object whose `body` decodes to the original bytes
