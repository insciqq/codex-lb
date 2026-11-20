## ADDED Requirements

### Requirement: Non-ASCII bridge signature headers are invalid signatures

The internal bridge route MUST treat a signature header value containing
non-ASCII characters as a non-matching signature and respond with the
`bridge_forward_invalid` error (400), never with a server error. A
non-ASCII tamper-proofing signature MUST fall through to the primary
signature verification like any other invalid tamper-proofing value.

#### Scenario: Garbled signature header

- **WHEN** an internal bridge request carries a signature header with a byte above 0x7F
- **THEN** the route responds 400 with `bridge_forward_invalid`
