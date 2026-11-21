# responses-api-compat Delta

## MODIFIED Requirements

### Requirement: Non-message system and developer input items are preserved

When normalizing Responses or compact request `input`, the service MUST only
hoist items that are system instruction messages — `system`-role items whose
`type` is omitted or `"message"` — into the top-level `instructions` field.
`developer`-role messages MUST remain in `input`, in their original position,
because `previous_response_id` carries `input` but not `instructions` into
chained responses. They receive the same input sanitization as other message
items: interleaved-reasoning keys (`reasoning_content`, `reasoning_details`,
`tool_calls`, `function_call`) and reasoning content parts are removed, because
upstream rejects them on every message role. A `developer` message with no
content left after part sanitization (`content` omitted or `null`, blank text,
no parts, or only parts the reasoning sanitizer removes) carries no
instruction and MUST be dropped rather than forwarded, including after the
`messages` alias turns empty content into a blank text part. `system` messages
are still hoisted, because upstream rejects them in `input`, so a `system`
message on a request chained through `previous_response_id` applies to that
request only. Any `system`/`developer`-role input item carrying any other `type`
value, including item types the service does not model, MUST be forwarded
upstream unchanged and in its original input position. This preservation MUST
hold both when the request is validated and when the request is serialized for
upstream delivery, and it exempts the item from input sanitization: keys such
as `reasoning_content`, `reasoning_details`, `tool_calls`, and `function_call`
MUST NOT be stripped from a preserved item. When compact requests exceed the
upstream input budget and
the service trims the input middle, preserved non-message `system`/`developer`
items MUST be treated as trim anchors and retained in the trimmed payload
rather than replaced by the trim marker. When a non-message
`system`/`developer` item is preserved and the request carries no top-level
`instructions` and no hoistable instruction messages, the service MUST default
`instructions` to the empty string so the request still validates and
forwards. The service MUST apply the same default when the request carries no top-level
`instructions` and its `input` holds a `developer` message. Requests whose input contains an `additional_tools` item remain
governed by the Responses Lite rule that leaves the entire input array and
top-level `instructions` unchanged.

#### Scenario: unknown non-message developer input item survives normalization

- **WHEN** a Responses or compact request `input` contains a typed, non-message
  item such as `{"type": "future_directive", "role": "developer", ...}`
  alongside developer instruction messages and user messages
- **THEN** the developer instruction messages remain in `input`
- **AND** the `future_directive` item remains in `input` unchanged, in its
  original position
- **AND** the upstream-serialized payload retains the item unchanged

#### Scenario: preserved directive keeps reasoning and tool-call keys

- **WHEN** a Responses or compact request `input` contains a typed,
  non-message `system`/`developer` item carrying keys the interleaved
  reasoning sanitizer strips from message items (such as
  `reasoning_content`, `reasoning_details`, `tool_calls`, or `function_call`)
- **THEN** the item is retained byte-identical after validation
- **AND** the upstream-serialized payload retains the item byte-identical

#### Scenario: directive-only request without instructions still validates

- **WHEN** a Responses or compact request omits top-level `instructions` and
  its `input` contains only a typed, non-message `system`/`developer` item
  (such as `{"type": "future_directive", "role": "developer", ...}`) alongside
  user messages
- **THEN** the request validates with `instructions` defaulted to `""`
- **AND** the directive item remains in `input` unchanged, including in the
  upstream-serialized payload

#### Scenario: preserved directive survives compact input trimming

- **WHEN** a compact request is large enough to trigger upstream input
  trimming and its input middle contains a typed, non-message
  `system`/`developer` item such as
  `{"type": "future_directive", "role": "developer", ...}`
- **THEN** the trimmed upstream payload retains the item unchanged
- **AND** the item is not replaced by the trim marker

#### Scenario: typeless system messages keep hoisting behavior

- **WHEN** an OpenAI-compatible client sends `input` containing
  `{"role": "system", "content": "sys"}` without a `type` field
- **THEN** that item is hoisted into `instructions` as before

#### Scenario: developer messages stay in input

- **WHEN** a Responses or compact request `input` contains
  `{"type": "message", "role": "developer", ...}` and user messages, with or
  without top-level `instructions`
- **THEN** the developer message remains in `input` unchanged, in its
  original position, including in the upstream-serialized payload
- **AND** `instructions` keeps its value, or defaults to `""` when absent

#### Scenario: developer messages without content are dropped

- **WHEN** a Responses or compact request `input` contains
  `{"role": "developer"}`, `{"role": "developer", "content": null}`, a developer
  message whose only text is blank, or one whose only part is a `reasoning` part
- **THEN** that item is not forwarded upstream
- **AND** the other input items keep their order

#### Scenario: developer messages are sanitized like other messages

- **WHEN** a Responses or compact request `input` contains a developer
  message carrying `reasoning_content`, `reasoning_details`, `tool_calls`,
  `function_call`, or a `reasoning` content part
- **THEN** the developer message remains in `input`, in its original position
- **AND** those keys and content parts are removed from it before forwarding

### Requirement: Responses-Lite replay proof tolerates only verified developer interleaving

When a fresh durable HTTP bridge classifies a client-unanchored Responses-Lite
full resend whose `additional_tools` bundle preserves developer messages inline,
the replay proof MUST tolerate a developer message only in the historical and
fresh positions defined below. Every other developer position or shape MUST
remain fail-closed.

A tolerated fresh developer message MUST have `type` omitted or equal to `message`,
MUST have role `developer`, MUST have no non-empty response-owned ID or phase,
MUST have no status or a `completed` status, MUST contain exact account-neutral
metadata with one nonblank `turn_id`, MUST contain exactly one self-contained
`input_text` content part, and MUST contain no unknown or account-scoped fields.
Explicit null or malformed item types MUST fail closed.

Classification MUST retain response-owned developer-message ID evidence until
these checks have completed, even when other response-owned IDs are projected
out. It MUST retain developer-role items before applying projection rules that
normally omit their declared item type, so a malformed developer item cannot
disappear before validation. A canonical Lite-prefix developer instruction MAY
appear immediately after the `additional_tools` bundle when it passes the same
account-neutral item checks as historical interleaving and has no response-owned
ID. A developer message in the stored prefix outside that canonical position or
the verified pending-call/matching-output interleave MUST fail closed. Outside
Responses Lite, developer messages stay inline, and the replay proofs MUST
judge such `input` without its account-neutral developer messages, as when
those were hoisted into `instructions`.

#### Scenario: Canonical Responses-Lite prefix remains transparent

- **GIVEN** a fingerprint-verified stored prefix begins with an `additional_tools` bundle
- **AND** a valid account-neutral developer instruction appears immediately after that bundle
- **WHEN** exact manifest or retained-output replay proof validates the stored prefix
- **THEN** the canonical developer instruction is transparent
- **AND** the original full input remains eligible for account-neutral replay

#### Scenario: Verified historical Responses-Lite developer message is transparent

- **GIVEN** a Responses-Lite input contains an `additional_tools` bundle
- **AND** its fingerprint-verified stored prefix contains a supported direct call
- **AND** a valid developer message appears before that call's matching output
- **AND** the fresh suffix exactly settles the durable pending-tool manifest
- **WHEN** the HTTP bridge opens a replacement session on the durable owner
- **THEN** it sends the original full input without injecting `previous_response_id`
- **AND** it sends the request once

#### Scenario: Other historical messages remain fail-closed

- **GIVEN** a supported direct call is pending in the verified stored prefix
- **WHEN** a user, assistant, system, malformed developer, or response-owned message appears before its output
- **THEN** exact manifest proof fails

#### Scenario: Other stored developer positions remain fail-closed

- **GIVEN** a fingerprint-verified stored prefix has no pending direct call
- **WHEN** a developer message appears outside the canonical adjacent Lite-prefix position
- **OR** the adjacent message has a response-owned ID
- **THEN** exact manifest and retained-output proofs fail

#### Scenario: Projection-omitted developer type remains visible to validation

- **GIVEN** a developer-role item declares a type normally omitted by replay projection
- **WHEN** account-neutral replay classification projects the full resend
- **THEN** the malformed developer item remains visible to replay proof
- **AND** replay classification fails closed

#### Scenario: Historical output remains mandatory

- **GIVEN** a valid developer message follows a supported historical call
- **WHEN** the matching output is missing or has another call ID or type
- **THEN** exact manifest proof fails

#### Scenario: Historical developer interleaving is bounded to one call and one message

- **GIVEN** a fingerprint-verified stored prefix opens a pending direct-call window
- **WHEN** that window holds more than one outstanding call at any point before the developer message
- **OR** a further call opens in that window after it has consumed a developer message
- **OR** a second developer message appears while the same window is still open
- **THEN** exact manifest proof fails
- **AND** a later window that holds exactly one outstanding call may still interleave one developer message

#### Scenario: Fresh developer suffix bounds are measured on the projected input

- **GIVEN** account-neutral replay classification projects the full resend
- **WHEN** the projection omits reasoning or completed bookkeeping items from the fresh suffix
- **THEN** the fresh developer suffix and terminality bounds are evaluated on the projected positions
- **AND** the accepted width is limited to shapes whose projected suffix satisfies those bounds

#### Scenario: Bounded fresh custom-tool developer interleave is transparent

- **GIVEN** the fingerprint-verified stored prefix is followed by a fresh suffix
- **AND** the durable pending-tool manifest contains exactly one `custom_tool_call`
- **WHEN** the entire suffix is exactly that custom call, one valid developer message, and its matching custom-tool output
- **THEN** exact manifest proof passes
- **AND** the original full input is sent once without injecting `previous_response_id`

#### Scenario: Other fresh tool-loop developer positions remain fail-closed

- **GIVEN** a durable pending-tool manifest
- **WHEN** a fresh developer message is used with a function or apply-patch call, appears in a parallel batch, is duplicated, lacks exact metadata, contains malformed or account-scoped content, or has leading or trailing suffix items
- **THEN** exact manifest proof fails

#### Scenario: Bounded retained-output developer follow-up is transparent

- **GIVEN** the fingerprint-verified stored prefix is followed by a completed assistant `final_answer`
- **AND** exactly one explicit user message follows that retained output
- **WHEN** one valid developer message is the terminal suffix item
- **THEN** retained-output proof passes
- **AND** the original full input is sent once without injecting `previous_response_id`

#### Scenario: Unproven retained-output developer follow-up remains fail-closed

- **GIVEN** a retained-output full resend
- **WHEN** the latest assistant output is not `final_answer`, the developer message is not terminal, the fresh input is raw or contains multiple user items, the developer metadata or content is not account-neutral, or the stored prefix contains historical developer interleaving
- **THEN** retained-output proof fails

## ADDED Requirements

### Requirement: Replay heuristics judge history without inline developer messages

Outside Responses Lite (`input` without an `additional_tools` item), developer
messages stay in `input` instead of moving into `instructions`, but they carry
no conversation history. Replay heuristics MUST judge the `input` through one
of two views, chosen by the direction in which each heuristic fails safe. The
forwarded payload MUST keep the developer messages in place, and Responses Lite
`input` MUST keep its own developer rules.

The classifier view serves every decision whether an `input` is a turn delta
or a complete conversation: the WebSocket client full-resend retry classifier,
the HTTP bridge full-resend classifier, and the bridge's decision whether a
stored-prefix trim leaves an unanchored body that is safe to retry (a trimmed
prefix of developer messages alone leaves a delta, so that decision stays as it
was before the trim). A classifier fails safe by calling
an `input` a delta, so this view MUST omit every developer message (`type`
omitted or `message`) whatever its shape (metadata, blank parts, status, extra
keys), exactly what the hoist removed.

The proof view serves the durable full-resend proofs (retained prior output and
pending tool-call manifest) and the compact account-neutral replay proof. A
proof fails safe by rejecting, so this view MUST omit only account-neutral
developer messages: role `developer`, `type` omitted or `message`, no phase, no
status or a `completed` status, only known fields, valid text content, and
metadata limited to a nonblank `turn_id` and nonblank `content_item_kinds`.
Any other developer item stays visible, and the proof rejects it. An item id
does not make such a message account-scoped, because upstream never emits
developer messages as response output. A verified stored prefix made only of
such developer messages MUST still count as a verified prefix, and the proof
MUST validate the retained output and fresh input that follow it. Without a
durable fingerprint, the compact proof MUST keep a history whose only items
ahead of the assistant output are developer messages owner-bound.

When an anchored continuation echoes the prior response's output before its
tool results and a developer message sits between them, the service MUST
still trim the recognized echoed output and MUST keep the developer message,
in order, ahead of the tool results.

#### Scenario: Developer and user delta stays anchored on a cold bridge
- **GIVEN** a hard bridge session with a durable latest response
- **WHEN** a client resumes with `input` of one developer message, of any shape, and one user message and no `previous_response_id`
- **THEN** the bridge sends the request with the durable anchor

#### Scenario: Developer and user delta is not replayed without its anchor
- **GIVEN** a WebSocket request with `previous_response_id` and `input` of developer messages, of any shape, and user messages
- **WHEN** upstream rejects the anchor before `response.created`
- **THEN** the proxy does not replay the input as a fresh conversation

#### Scenario: Non-Lite full resend with a developer prefix keeps its proof
- **GIVEN** a durable stored prefix of a developer message (with or without Codex content-kind metadata), optionally followed by a user message
- **WHEN** a client resends that prefix, the prior assistant output, and a new user message
- **THEN** the retained-output proof succeeds as it did when the developer message was hoisted
- **AND** the bridge sends the full resend without the durable anchor

#### Scenario: Developer update between echoed output and tool result
- **WHEN** an anchored continuation sends echoed reasoning, assistant output, and tool call, then a developer message, then the tool result
- **THEN** the upstream `input` is the developer message followed by the tool result
