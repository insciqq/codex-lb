## ADDED Requirements

### Requirement: Upstream transport failures keep their cause in the logs

When the native egress helper fails an upstream HTTP request, its error event
MUST carry, in addition to the static message and failure phase, a bounded
description of the underlying transport error chain rendered only from typed
fields of known error types. It MUST NOT copy free-form error text that can
carry the request URL or peer-supplied data, including h2 GOAWAY debug data.
The proxy MUST log that description with the failure phase and request id, and
MUST NOT add it to the error message returned to the client. When a
per-request upstream websocket closes before the stream's terminal event, the
proxy MUST log the close code and message type, at warning level when the peer
closed or the connection was lost and at debug level after a local close. It
MUST log the peer's close reason only when the reason is one of a fixed set of
generic phrases, and otherwise a placeholder. Request bodies and authorization
headers MUST NOT be logged.

#### Scenario: Native HTTP failure is logged with its cause

- **GIVEN** the native helper reports an upstream body failure with a cause chain
- **WHEN** the proxy receives the error event
- **THEN** a warning log line carries the failure phase and the cause description
- **AND** the transport error message seen by callers is the static message

#### Scenario: GOAWAY debug data stays out of the detail

- **GIVEN** the upstream sends an HTTP/2 GOAWAY whose debug data holds a signed URL
- **WHEN** the helper reports the failed request
- **THEN** the detail names the h2 GOAWAY and its reason code
- **AND** it does not contain the debug data

#### Scenario: Upstream websocket close before terminal is logged with its code

- **GIVEN** an upstream websocket stream has not produced a terminal event
- **WHEN** the upstream closes it with code 1011 and a generic reason
- **THEN** a warning log line carries close code 1011 and the reason

#### Scenario: A free-text close reason is not logged

- **GIVEN** an upstream websocket stream has not produced a terminal event
- **WHEN** the upstream closes it with a reason quoting a signed URL or request content
- **THEN** the log line carries the close code and a placeholder instead of the reason
