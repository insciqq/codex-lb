## ADDED Requirements

### Requirement: A client disconnect while an encoded body is read ends the request quietly

When the client disconnects before it has sent the whole of a `Content-Encoding` request body, the decompression stage MUST end the request without calling the route and without sending a response, and MUST NOT let the disconnect escape the application as an error. The disconnect MUST leave a debug-level log entry with the method and path. A failure to read the body other than a client disconnect MUST still propagate.

#### Scenario: Client leaves mid-body

- **GIVEN** a client sends a gzip-encoded POST with `more_body` set
- **WHEN** the next receive reports `http.disconnect`
- **THEN** the route is not called and no response is sent
- **AND** no exception escapes the application
- **AND** a debug-level entry records the aborted upload

#### Scenario: Other body read failures still propagate

- **GIVEN** a client sends an encoded POST
- **WHEN** reading the body fails with an error that is not a client disconnect
- **THEN** that error propagates as before
