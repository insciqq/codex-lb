## ADDED Requirements

### Requirement: Large response.create bodies are prepared without redundant full copies

When the service prepares a `response.create` body, it MUST measure the
serialized size used for the transport decision, the upstream websocket budget
and historical slimming without holding another full serialization or
encoding of the body, and the measured size MUST equal the byte length of the
compact serialization. A body within the websocket budget MUST be serialized
in the same pass that measures it, and a body over the budget MUST be
serialized only once, after historical slimming. Input fingerprints MUST equal the
SHA-256 of the canonical whole-list dump while hashing it one item at a time.

#### Scenario: Oversized websocket frame is slimmed without full copies

- **GIVEN** a websocket `response.create` of about 50 MB whose history carries inline screenshots
- **WHEN** the service prepares it for the upstream websocket
- **THEN** the forwarded frame has its historical screenshots replaced by omission notices and keeps the latest user turn
- **AND** beyond the parsed frame, the preparation allocates less than the websocket budget plus twice its largest input item

#### Scenario: Routine body is encoded once

- **WHEN** a `response.create` within the websocket budget is prepared on the bridge or websocket path
- **THEN** its body is encoded once, and that encoding is the forwarded frame

#### Scenario: Sizes and digests are unchanged

- **WHEN** a body or an input list is measured or fingerprinted
- **THEN** the size equals the length of its compact `ensure_ascii` serialization
- **AND** the digest equals the SHA-256 of the canonical whole-list dump
