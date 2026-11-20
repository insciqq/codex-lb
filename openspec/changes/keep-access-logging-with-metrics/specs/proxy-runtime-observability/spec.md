## ADDED Requirements

### Requirement: Metrics server keeps the server logging configuration

Starting the in-process metrics server MUST NOT change process-wide logging:
the configured handlers and levels of the `uvicorn.*` loggers MUST stay in
effect, and access-log lines MUST continue to be emitted. JSON access-log
records MUST carry the client address, the redacted request line and the
response status.

#### Scenario: Access lines survive metrics server startup

- **GIVEN** metrics are enabled
- **WHEN** a client requests `GET /health` after the metrics server has started
- **THEN** the server emits an access-log line for that request

#### Scenario: JSON access line fields

- **GIVEN** the JSON log format
- **WHEN** a client requests `GET /health`
- **THEN** the access record contains the client address, the request line `GET /health HTTP/1.1` and status 200
