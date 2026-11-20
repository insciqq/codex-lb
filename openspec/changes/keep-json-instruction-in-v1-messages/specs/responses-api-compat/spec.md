## ADDED Requirements

### Requirement: The v1 messages alias follows the Responses input instruction rules

When a `/v1/responses` or `/v1/responses/compact` request supplies its conversation through `messages`, the service MUST apply the same instruction-message rules as for `input`: `developer` messages stay in `input` in their original position, `system` messages are hoisted into `instructions`, and for a `/v1/responses` request with `text.format.type = "json_object"` a `system` message that mentions JSON stays in `input` with the `developer` role.

#### Scenario: System JSON instruction sent through messages
- **WHEN** a JSON-mode `/v1/responses` request sends a system message `Reply with a JSON object.` and a user message `Say hello.` through `messages`
- **THEN** the upstream `input` starts with a `developer` message `Reply with a JSON object.`, followed by the user message
- **AND** `instructions` do not contain `Reply with a JSON object.`

#### Scenario: Developer message sent through messages
- **WHEN** a `/v1/responses` or `/v1/responses/compact` request sends a developer message and a user message through `messages`
- **THEN** the upstream `input` keeps the developer message before the user message
- **AND** `instructions` do not contain the developer message

#### Scenario: Messages without JSON mode
- **WHEN** a `/v1/responses` request without JSON mode sends system messages through `messages`
- **THEN** every system message is hoisted into `instructions`
