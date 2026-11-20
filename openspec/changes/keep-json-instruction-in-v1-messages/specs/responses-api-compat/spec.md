## ADDED Requirements

### Requirement: JSON mode keeps a JSON instruction from the v1 messages alias

When a `/v1/responses` request with `text.format.type = "json_object"` supplies its conversation through `messages`, the service MUST apply the same JSON-mode instruction rule as for `input`: a `system` or `developer` message that mentions JSON stays in `input` with the `developer` role, and other instruction messages are hoisted into `instructions`.

#### Scenario: System JSON instruction sent through messages
- **WHEN** a JSON-mode `/v1/responses` request sends a system message `Reply with a JSON object.` and a user message `Say hello.` through `messages`
- **THEN** the upstream `input` starts with a `developer` message `Reply with a JSON object.`, followed by the user message
- **AND** `instructions` do not contain `Reply with a JSON object.`

#### Scenario: Messages without JSON mode
- **WHEN** a `/v1/responses` request without JSON mode sends system messages through `messages`
- **THEN** every system message is hoisted into `instructions`
