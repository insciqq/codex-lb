## ADDED Requirements

### Requirement: Encoded request bodies are decoded without extra full copies

The request-decompression stage MUST accumulate the received encoded body and
each decoded representation without converting a finished buffer into a
second full copy, MUST bound every piece a deflate layer outputs, and MUST
NOT keep the encoded body, or the decoded body once it has been handed to the
route, alive for the rest of the request.

#### Scenario: Large encoded body reaches the route

- **WHEN** a 16 MiB request body arrives encoded with `zstd`, `gzip` or `deflate`
- **THEN** the route receives the exact decoded body
- **AND** decoding allocates less than one and a half times the decoded size plus the encoded size
