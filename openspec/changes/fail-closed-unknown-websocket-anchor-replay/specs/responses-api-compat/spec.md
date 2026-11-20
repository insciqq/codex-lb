## MODIFIED Requirements

### Requirement: WebSocket full-resend previous-response misses retry without stale anchor
When a direct WebSocket `response.create` request carries a `previous_response_id` that the service injected, the service MUST retain the client's original unanchored body as a replay body. If upstream rejects that anchor with `previous_response_not_found` before `response.created`, the service MUST reconnect and replay the retained body as a fresh turn instead of forwarding the raw upstream invalid-request error. A body that only carries incremental tool outputs for tool calls that are not also present in the same body is not self-contained and MUST NOT be replayed as a fresh turn.

When the request carries a client-supplied `previous_response_id`, the service MUST NOT retain or send any replay body without that anchor, whatever the input contains. Clients such as Codex send their own anchor with only the incremental input, and the service cannot verify that any input restates the anchor's whole context, so an anchorless replay could silently drop earlier context. A pre-output `previous_response_not_found` for such a request MUST reach the client as the sanitized canonical `previous_response_not_found` code, on the Codex-native and the public route alike, so that the client resends its complete input without the anchor.

#### Scenario: proxy-injected anchor misses before output
- **WHEN** the service injected `previous_response_id` into a WebSocket `/v1/responses` or `/backend-api/codex/responses` request whose client body carried no anchor
- **AND** upstream emits `previous_response_not_found` before assigning a response id
- **THEN** the service reconnects the upstream WebSocket
- **AND** it replays the client's original body without `previous_response_id`
- **AND** the downstream client receives the recovered response events, not the raw `previous_response_not_found` error

#### Scenario: full-resend WebSocket follow-up loses just-completed anchor
- **WHEN** a WebSocket `/v1/responses` or `/backend-api/codex/responses` follow-up has a client-supplied `previous_response_id` naming the response this connection just completed
- **AND** its input restates the whole history
- **AND** upstream emits `previous_response_not_found` before assigning a response id
- **THEN** the service MUST NOT replay that input without `previous_response_id`
- **AND** the downstream client receives the sanitized `previous_response_not_found` code and resends without the anchor

#### Scenario: output-only WebSocket tool delta is not replayed as a fresh turn
- **WHEN** a WebSocket `/v1/responses` or `/backend-api/codex/responses` request carries `function_call_output`, `custom_tool_call_output`, or `apply_patch_call_output` items without their matching tool-call items in the same payload
- **THEN** the service MUST NOT replay that payload as a fresh turn without `previous_response_id`

#### Scenario: client-supplied anchor is never replayed without it
- **WHEN** a WebSocket `/v1/responses` or `/backend-api/codex/responses` request has a client-supplied `previous_response_id`
- **AND** its input is a delta or restates the whole history
- **THEN** the service MUST NOT retain an anchorless replay body for that request
- **AND** if upstream emits `previous_response_not_found` before assigning a response id, the downstream client receives the sanitized `previous_response_not_found` code rather than a fresh turn
