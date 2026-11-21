# Responses API Compatibility Context

## Purpose and Scope

This capability implements OpenAI-compatible behavior for `POST /v1/responses`, including request validation, streaming events, non-streaming aggregation, and OpenAI-style error envelopes. The scope is limited to what the ChatGPT upstream can provide; unsupported features are explicitly rejected.

See `openspec/specs/responses-api-compat/spec.md` for normative requirements.

## Idle Codex upstream loss

After a completed response, an idle physical upstream can lose the context
that the client's next delta assumes. The proxy signals that loss by closing
the Codex downstream with code 1012. A connection identity protects a newer
shared continuity anchor from being retired by an older connection's close.
Durable account ownership remains intact.

For example, after response A completes, its upstream closes while no new
request is pending. The downstream reconnects and sends the full conversation
without an injected response-A anchor. This does not recover context that the
client no longer has; a delta alone must still follow existing fail-closed
continuity rules. Public OpenAI-compatible routes and active requests retain
their existing lifecycle behavior.

The notice arms once the client has seen a response id from the upstream: a
`response.created`, or the terminal of a transparent replay that suppressed its
own `response.created`. A crashed reader with nothing in flight triggers it too;
requests already sent on that upstream fail as on a transport end, while a
registered request still waiting to be sent stays with its sender, which moves
it to a new upstream.

A turn received during an idle close may still be awaiting preparation or admission. Registration shares the pending lock with the close decision and checks the disconnected state before dispatch; the registered state then uses normal scope cleanup.

## Client-supplied anchors are never replayed without them

A client that sends its own `previous_response_id` either sends the incremental
input (Codex does) or a history the proxy cannot verify against the anchor's
real output. Replaying such a request without the anchor could silently drop
earlier context, so the proxy never does: a stale or unknown client anchor
surfaces `previous_response_not_found`, and the client resends its complete
input without the anchor, as it would against upstream. Public `/v1/responses`
WebSocket clients get the same sanitized code here instead of the usual
`stream_incomplete` mask, which would make them retry the same anchor. Anchors
the proxy injected itself are different: the client's own unanchored body is
complete by definition, so a stale injected anchor is still recovered by
replaying that body.

## Rationale and Decisions

- **Responses as canonical wire format:** Internally we treat Responses as the source of truth to avoid divergent streaming semantics.
- **Strict validation:** Required fields and mutually exclusive fields are enforced up front to match official client expectations.
- **Cursor alias compatibility:** Cursor UI model labels may append reasoning or speed suffixes to GPT-5 slugs; those are normalized to canonical upstream fields before forwarding.
- **No truncation support:** Requests that include `truncation` are rejected because upstream does not support it.
- **Compact as a separate contract:** Standalone compact is treated as a canonical opaque context-window contract, not as a variant of buffered normal `/responses`.

## Constraints

- Public-contract SSE filtering uses the `response.*` and `error` families, so
  diagnostics such as `responsesapi.websocket_timing` cannot interrupt strict
  client event deserializers. For example, a timing diagnostic between a text
  delta and `response.completed` is removed while both standard events remain.
  Native Codex requests retain vendor events; OpenAI-shaped backend requests
  follow public filtering. This does not normalize string-valued
  `response.instructions` or establish full IntelliJ compatibility (Refs #1934).

- Upstream limitations determine available modalities, tool output, and overflow handling.
- `store=true` is rejected; responses are not persisted.
- `include` values must be on the documented allowlist.
- `truncation` is rejected.
- `previous_response_id` is forwarded when `conversation` is absent, but the `conversation + previous_response_id` conflict remains rejected.
- HTTP `/v1/responses` and HTTP `/backend-api/codex/responses` now use a server-side upstream websocket session bridge by default so repeated compatible requests can keep upstream response/session continuity without forcing clients onto the public websocket route.
- Codex-affinity HTTP bridge sessions can optionally use a conservative first-request prewarm (`generate=false`), but that behavior stays behind an explicit switch so production defaults do not pay an extra upstream request unless operators opt in. The switch is the dashboard setting `http_responses_session_bridge_codex_prewarm_enabled` (Settings → Advanced → Session bridge), resolved from the settings-cache snapshot before a session's prewarm lock; its `CODEX_LB_*` environment variable is a deprecated alias that applies only while the dashboard value is unset.
- When operators configure a multi-instance bridge ring, deterministic owner enforcement now applies only to hard continuity keys such as `x-codex-turn-state` and explicit session headers. Prompt-cache-derived bridge keys remain stable for local reuse, but in gateway-safe mode a non-owner replica may tolerate that locality miss and create or reuse a local session instead of failing with `bridge_instance_mismatch`.
- Codex-facing websocket routes now advertise `x-codex-turn-state` during websocket accept and honor client-provided turn-state on reconnect so routing can stay sticky at turn granularity even when the public websocket reconnects.
- HTTP responses routes now also return `x-codex-turn-state` headers so clients that persist response headers can promote later HTTP requests from prompt-cache affinity to stronger Codex-session continuity.
- `/v1/responses/compact` keeps a final-JSON contract and preserves the raw upstream `/codex/responses/compact` payload shape as the canonical next context window instead of rewriting it through buffered `/codex/responses` streaming.
- Compact transport failures fail closed with respect to semantics: no surrogate `/codex/responses` fallback and no local compact-window reconstruction.
- Compact transport may use bounded same-contract retries only for safe pre-body transport failures and `401 -> refresh -> retry`.
- `/v1/responses/compact` is supported only when the upstream implements it.
- `prompt_cache_key` affinity on OpenAI-style routes is intentionally bounded by a dashboard-managed freshness window, unlike durable backend `session_id` or dashboard sticky-thread routing.
- Codex-native direct websocket `/backend-api/codex/responses` treats upstream `previous_response_id` as an ephemeral anchor. If that anchor goes stale, the proxy masks raw upstream details and emits the sanitized canonical `previous_response_not_found` classifier so compatible Codex clients can retry with full local history and no `previous_response_id`. The upstream Codex socket has emitted this condition both with the canonical code and as a parameterless `invalid_request_error` carrying ``Invalid `previous_response_id`.``; both shapes use the same recovery policy.
- Upstream Responses WebSockets use transport ping/pong control frames to detect a black-holed connection without confusing valid application-event silence with an idle turn. Direct and routed connections reuse `proxy_downstream_websocket_idle_timeout_seconds` for this zero-config liveness budget.
- A post-send liveness timeout is delivery-ambiguous. It remains account-neutral, is never transparently replayed, and retires the affected upstream socket so a client retry opens a fresh route without risking duplicated model work or tool side effects.
- An HTTP SSE first-event `stream_idle_timeout` is also account-neutral for health writes. The request may still exclude that account and fail over, but idle silence must not increment `error_count` or move the account into probe/drain.
- HTTP bridge settlement ownership is explicit: `closed` rejects new work but does not imply that a submitter owns existing siblings. Only a liveness-failed send claims whole-deque settlement under the lifecycle lock; otherwise the reader remains responsible for settling pending requests when the transport dies.
- A DRAINING durable row with a live lease is still owned. Foreign `claim_live_session` and local session create must not steal it, including when forced recovery would otherwise run because the owner endpoint is missing; expired or ownerless DRAINING rows remain recoverable.
- Hard-affinity retry-circuit evidence is request-lifecycle evidence: retirement counts only while the bridge still owns an eventless pending request. Idle no-pending retirement remains observable but neutral, so routine socket churn cannot manufacture the first strike for a later real timeout.
- A retry-circuit probe follows only a cooldown expiry the worker observed. A durable row whose cooldown is absent, zero or already elapsed maps to no cooldown, so an old cooldown first seen after it ended admits requests normally instead of leasing one probe; at the failure threshold that lease suppressed the key's other requests for up to the whole lease, and below it (a settled key's zero row included) it logged spurious `half_open` events (issue 2272).
- Quarantine fences identify evidence, not entries: generations come from one service-lifetime sequence and every arm or eventless strike advances them, so a completion clears only what it observed. Only the session registered for the key may clear its entry, so a late completion on a retired session cannot clear a quarantine its replacement earned, while the replacement's own healthy completion still clears what forced it onto the fresh path (issue 2268).
- Known limitations of the retry circuit and quarantine (accepted, not regressions):
  - A durable load samples the monotonic clock before its lookup and the wall clock after it, so a cooldown with only a few milliseconds left can be judged already ended (or still running) by the lookup's database latency.
  - Clearing a matched poison quarantine removes the whole entry, so a first eventless strike recorded on that key while the verified recovery was in flight is dropped with it; the poison fence deliberately ignores newer weaker evidence.
  - When an owed settlement's fenced resets both miss and the state is reconciled onto an unchanged surviving episode whose cooldown already ended, a half-open lease armed before the settlement stays until it lapses. Only a deployment with several replicas writing the same key reaches this.

## Fast Mode and Service Tiers

codex-lb accepts the OpenAI/Codex `service_tier` field on Responses and Chat
Completions compatible routes. The legacy `fast` spelling is accepted as an
alias and is forwarded upstream as the canonical `priority` tier.

Fast Mode is request-level intent, not a local speed guarantee. The upstream
Codex backend decides the actual tier for each completed response. codex-lb
therefore records three separate values in request logs:

- `requestedServiceTier`: what the client or API key asked for, after alias
  normalization.
- `actualServiceTier`: what upstream reported in the completed response, when
  upstream included it.
- `serviceTier`: the effective billable tier. This uses `actualServiceTier`
  when present and falls back to `requestedServiceTier` only when upstream omits
  the actual tier.

If a request is sent with `service_tier: "fast"` or `service_tier: "priority"`
and the completed row shows `requestedServiceTier: "priority"` but
`actualServiceTier: "default"`, codex-lb forwarded the priority request and
upstream chose the default tier. That can happen even when websocket transport
is active.

For OpenCode or Codex-compatible clients, enable Fast Mode by sending a
Responses request with:

```json
{
  "service_tier": "priority"
}
```

Clients that expose Fast Mode as `fast` may keep using that spelling; codex-lb
normalizes it to `priority` before forwarding.

### Ultrafast Processing

The [OpenAI Responses API reference](https://developers.openai.com/api/reference/resources/responses/methods/create)
documents `ultrafast` as an access-controlled processing tier currently
available for `gpt-5.6-sol`. codex-lb forwards this canonical value unchanged;
it does not grant Ultrafast access by itself.

Account eligibility comes from live or retained per-account upstream catalog
metadata. The bundled bootstrap catalog deliberately does not advertise
Ultrafast. If no account advertises the tier, an explicit Ultrafast request
cannot select an eligible account; API-key enforcement follows the existing
model-capability fallback when the model itself does not advertise the tier.

Send a Responses request with:

```json
{
  "model": "gpt-5.6-sol",
  "input": "Summarize the change.",
  "service_tier": "ultrafast"
}
```

After completion, verify that the response reports
`service_tier: "ultrafast"`. Request logs retain `ultrafast` in the requested,
actual, and effective billable tier fields when upstream confirms it.

### Operator Fast Mode prohibition

Operators can enable the Routing setting `prohibitFastMode` when qualified
Codex harness model aliases such as `gpt-5.6-sol-xhigh-fast` must run at the
normal OpenAI tier. The alias still supplies its canonical model and reasoning
effort, but does not derive `service_tier: "priority"`. This policy does not
rewrite an explicit client tier or an API-key-enforced tier; see
`openspec/specs/fast-mode-policy/context.md` for scope and operating notes.

API keys can also force the tier for traffic that uses that key. Set the key's
enforced service tier to `priority` or `fast`; both values are stored and
returned as `priority`.

To verify a completed Fast Mode request:

1. `Transport` should be `WS` if you are verifying the websocket Codex path.
2. `requestedServiceTier` should be `priority` when the client requested Fast
   Mode or the API key enforced it.
3. `actualServiceTier` is the upstream result. `default` means upstream did not
   grant priority for that response.

This distinction matters for quota and cost accounting: codex-lb prices the
request from the effective billable `serviceTier`, not from the requested tier
when upstream reports a different actual tier.

## Include Allowlist (Reference)

- `code_interpreter_call.outputs`
- `computer_call_output.output.image_url`
- `file_search_call.results`
- `message.input_image.image_url`
- `message.output_text.logprobs`
- `reasoning.encrypted_content`
- `output.encrypted_content`
- `web_search_call.action.sources`

The output selector is additive: Codex can request it alongside reasoning
encryption. Its [client request construction](https://github.com/openai/codex/blob/33d306d457a4632b22bebf214e0c25d91f90f6e8/codex-rs/core/src/client.rs#L1130)
adds `output.encrypted_content` when output token replay is enabled for the
OpenAI provider. The [ResponseItem model](https://github.com/openai/codex/blob/33d306d457a4632b22bebf214e0c25d91f90f6e8/codex-rs/protocol/src/models.rs#L1052)
also carries ciphertext on messages and tool calls.

The proxy forwards these fields as opaque values. For example, a returned
message with `id: "msg_1"`, `phase: "final_answer"` and
`encrypted_content: "opaque+/="` retains those fields when sent back in the
next turn. Item IDs and encrypted function arguments travel with the content;
the proxy does not decrypt it or treat it as account-neutral replay state.
Synthetic transport tests verify field preservation, not provider-side
cryptographic validity. Unknown selectors and malformed include arrays still
fail validation before dispatch.

## Failure Modes

- **Stream ends without terminal event:** Emit `response.failed` with `stream_incomplete`.
- **Upstream error / no accounts:** Non-streaming responses return an OpenAI error envelope with 5xx status.
- **Compact upstream transport/client failure:** Retry only inside `/codex/responses/compact` when the failure is safely retryable; otherwise return an explicit upstream error without surrogate fallback.
- **HTTP bridge session closes or expires:** The next compatible HTTP `/v1/responses` or `/backend-api/codex/responses` request recreates a fresh upstream websocket bridge session; continuity is guaranteed only within the lifetime of one active bridged session.
- **Multi-instance routing without bridge owner policy:** if operators do not configure a bridge ring or front-door affinity, continuity can still fragment across replicas. With a configured bridge ring, hard continuity keys landing on a non-owner replica are proxy-forwarded to the owner replica; the proxy fails closed only when the owner endpoint or ring membership cannot be resolved or the forward signature fails authentication. Gateway-safe prompt-cache requests may accept locality misses and continue locally instead of forwarding.
- **Codex websocket reconnects:** Reconnect continuity now depends on the client replaying the accepted `x-codex-turn-state`; generated turn-state is emitted on accept for backend Codex routes and echoed back when the client already supplies one.
- **Codex websocket stale previous-response anchors:** Direct backend Codex websocket stale-anchor failures are either replayed transparently from a self-contained full resend or surfaced as a sanitized `response.failed` whose `response.error.code` is `previous_response_not_found`; the error omits `param`, the raw upstream envelope, and the missing `resp_...` id. A connect-time failure uses the same code directly at `error.code`. This includes the parameterless upstream message ``Invalid `previous_response_id`.``. OpenAI-compatible `/v1/responses` websocket clients continue to receive generic `stream_incomplete` masking.
- **Websocket handshake forbidden/not-found:** Auto transport now fails loud on `403` / `404` instead of silently hiding the websocket regression behind HTTP fallback.
- **Upstream websocket stops answering pings:** Pending direct-WebSocket and HTTP-bridge work fails with `upstream_websocket_liveness_timeout`; the account remains healthy and the request is not replayed because upstream acceptance is unknown.
- **Repeated eventless bridge failures:** Two consecutive request-affecting pre-response failures can open the hard-key cooldown. A successful terminal response clears the state; an idle close followed by one real timeout remains only one strike.
- **Invalid request payloads:** Return 4xx with `invalid_request_error`.

## Error Envelope Mapping (Reference)

- 401 → `invalid_api_key`
- 403 → `insufficient_permissions`
- 404 → `not_found`
- 429 → `rate_limit_exceeded`
- 5xx → `server_error`

## Examples

Non-streaming request/response:

```json
// request
{ "model": "gpt-5.1", "input": "hi" }
```

```json
// response
{ "id": "resp_123", "object": "response", "status": "completed", "output": [] }
```

Cursor-style model alias request:

```json
{ "model": "gpt-5.4-mini-high", "input": "hi" }
```

This forwards upstream as `model: "gpt-5.4-mini"` with `reasoning.effort: "high"`.

Retry-circuit accounting example: an idle bridge closes with `pending=0`, then
the next request times out before `response.created`. The idle close is logged
but contributes no failure; the timeout is the first strike. Only another
consecutive eventless pending failure may open the repeated-failure cooldown.

Stale-anchor recovery example: a reconnect sends a tool-output delta with a
recent `previous_response_id`, and upstream answers
``{"type":"error","status":400,"error":{"type":"invalid_request_error","message":"Invalid `previous_response_id`."}}``.
Because the delta cannot stand alone, codex-lb returns a sanitized
`previous_response_not_found` signal on the Codex-native route so the client can
retry once with full local history. If the original request already contained a
self-contained full resend, codex-lb instead reconnects and replays that body
without the rejected anchor.

## Previous-response replay owner fencing

Removing a stale continuation anchor does not make every retained body
portable. Encrypted reasoning and other retained input establish a dispatch
owner once an upstream attempt is accepted or has an ambiguous outcome. File
references, continuation state, and durable bridge operation identities remain
independently owned. The proxy requires established owners on later HTTP
streaming, HTTP bridge, and direct WebSocket selections.

For example, if account A receives encrypted reasoning and returns a
pre-visible Trusted Access or authentication failure, account B must never
receive the retained ciphertext. One forced token refresh may replay the body
on account A; permanent failure or owner unavailability fails closed. A
classified pre-visible rate-limit or quota rejection is narrower: it does not
establish the pending dispatch owner only when ciphertext is the body's sole
account-scoped retained state, so the exact unchanged ciphertext may be retried
on account B when no independent owner exists. A file, container, vector-store,
nonneutral URL, unknown retained item, or other non-ciphertext account-scoped
reference keeps the body on account A even if its durable owner cannot be
resolved.

Controlled upstream probes have accepted valid encrypted reasoning across
distinct subscription identities while rejecting modified ciphertext. That is
compatibility evidence, not a documented cryptographic, account-portability, or
policy guarantee. Cross-account submission may be observable upstream, and the
behavior may change; the proxy therefore limits it to the existing classified
pre-visible failover path rather than treating encrypted input as generally
portable. If the alternate account returns literal `invalid_encrypted_content`
or the observed reasoning-decryption rejection shape for retained encrypted
reasoning or compaction, the proxy writes a dedicated
`cross_account_encrypted_reasoning_rejected` warning with request,
source-account, target-account, trigger, and error-code provenance. It never
logs encrypted content; operators can search this event directly to detect an
upstream portability change. The request-shaped rejection does not increment
the target account's health error count.

Verified recovery installs a replacement body and updates owner state
atomically. A canonical account-neutral replacement clears the owner and may
use normal failover. A verified nonneutral replacement, including a
Responses-Lite full resend, may replay only on the same owner and preserves the
fence.

HTTP bridge tracing archive IDs do not pin neutral requests. A real durable
`operation_id` does pin the request until an explicit operation-rebind path
replaces that identity. Existing file pins and API-key settlement-before-health
ordering remain independent invariants.

Streaming selection authorizes owner compatibility before opening upstream, but
persists a new owner only after dispatch is observed. A transport failure that
is positively classified as pre-dispatch therefore leaves the body unowned and
eligible for its first real dispatch on another account. Ambiguous failures
remain owner-bound.

## Known Client Integrations (Reference)

Third-party agents that consume the `/v1` Responses surface documented by this
capability (rendered guide: `docs/client-setup.md`). These are configuration
examples against the existing contract, not separate compatibility surfaces:

- **OpenCode** — built-in `openai` provider with a `baseURL` override; uses the
  Responses API path so `encrypted_content` / multi-turn reasoning state is
  preserved (Chat Completions custom providers drop it).
- **OpenClaw** — custom provider with `"api": "openai-responses"` against
  `/v1`; Codex-native provider builds may target `/backend-api/codex` instead.
- **Hermes Agent** (Nous Research) — named custom provider with
  `api_mode: codex_responses` against `/v1`; the responses transport carries
  reasoning state across turns like the OpenCode path.

New client guides added to `docs/client-setup.md` should stay configuration-only
examples of this contract; anything needing new proxy behavior requires its own
OpenSpec change first.

## Operational Notes

- Pre-release: run unit/integration tests and optional OpenAI client compatibility tests.
- Smoke tests: stream a response, validate non-stream responses, and verify error envelopes.
- Post-deploy: monitor `no_accounts`, `upstream_unavailable`, compact retry attempts, and compact failure phases, especially on direct compact requests.
- Post-deploy: monitor HTTP bridge reuse/create/evict/reconnect counts and any `previous_response_not_found` or queue-saturation errors on `/v1/responses` and `/backend-api/codex/responses`.
- Post-deploy: monitor `capacity_exhausted_active_sessions`, Codex-session bridge reuse/evict counts, websocket handshake 403/404 rates after the narrower auto-fallback policy, and backend Codex HTTP vs websocket cache-ratio gaps.
- When tracing compact incidents, confirm that request logs and upstream logs show direct `/codex/responses/compact` usage without surrogate `/codex/responses` fallback.
- Post-deploy: monitor `no_accounts`, `stream_incomplete`, and `upstream_unavailable`.
- Post-deploy: monitor `upstream_websocket_liveness_timeout`; recurring failures indicate a host route, VPN, proxy, or intermediary that black-holes established WebSockets.
- Post-deploy: correlate retry-circuit `opened`, `half_open`, and `reset` events with bridge `pending` and `response_events_seen` diagnostics. An idle `pending=0` retirement must not precede an immediate two-failure cooldown.
- Post-deploy: monitor `previous_response_not_found` on `/backend-api/codex/responses`; recurring spikes show repeated continuity failures, which may come from malformed client identifiers, server-side invalidation, or connection lifecycle. Clients should perform the documented full-context retry without `previous_response_id`. Investigate socket-lifecycle remediation only when a separate close-reason, reconnect, or transport diagnostic correlates with the failures.
- Websocket/Codex CLI tier verification runbook: `openspec/specs/responses-api-compat/ops.md`


## HTTP continuation promotion

Healthy native HTTP requests use normal policy. The proxy cannot infer every
client-local WebSocket failure from HTTP alone; it uses its existing 60-second
upstream-connect failure marker as concrete failure evidence. Operator HTTP
pins and size bypasses remain effective. The image bypass keeps requests off the
HTTP session bridge but no longer pins the upstream transport, which is resolved
by ordinary precedence; an `input_image` request keeps upstream HTTP only when
its payload exceeds the WebSocket frame budget or still carries an external
image URL. External-URL detection for that decision recurses the whole input, so
a URL nested inside a tool-output array keeps the pin even though the image
inliner never rewrites it — that is the case where the URL is still external at
the upstream. The inliner and the bridge's post-inline guard still read only
top-level `input_image` items and one level of `content`; closing that is a
separate change. No new retry/session registry.

History-only locality is soft, scoped by the bridge's full API-key identifier,
and hashes the complete first user item plus instructions and model. No client
prompt cache field is overwritten. Identical initial prompts may share an idle
connection, but neither histories nor response anchors are merged; the complete
request is sent each time. Existing hard-continuity paths retain their guarded
incremental replay. Conversation IDs get their own hashed locality and are never
combined with an injected previous_response_id.

Chat keeps the existing stream conversion/usage/error/cleanup pipeline and uses
the bridge only after the source-routing branch. Backend stream=false retains
its native non-streaming upstream contract. No claimed latency percentage:
connection reuse is measured separately from admission and successful transport.

For example, a Chat client sending `[user(task), assistant(answer), user(next)]`
without session headers can open a bridge connection. Appending the next
assistant/user pair reuses that connection while sending the entire new history.
The same initial task under a different API key selects a separate connection.

Chat binds existing settlement ownership signals while advancing its bridged
stream. Predispatch failures and cancellation release origin-owned reservations;
accepted or delivery-ambiguous owner forwards retain their settlement owner.
Context bindings do not span yields because startup probes and consumers may
advance the stream from different tasks.

## HTTP response ownership before delivery

An HTTP response can expose its upstream ID before the detached request-log write finishes. The stream now publishes each authoritative lifecycle ID to the existing bounded process cache before delivering that event, beginning with `response.created` when present. An immediate follow-up can resolve the selected account while the original stream or its log write is still pending. This is same-process owner readiness; it does not promise that an unfinished response is already usable by the upstream provider.

The cache retains its existing API-key partition, session-first lookup and same-key fallback. Durable lookup and unknown-owner rejection remain the miss path. Publication adds no synchronous persistence barrier, registry or cross-replica readiness guarantee, and does not strengthen session identifiers into a new authorization boundary.

Local failure events and locally assigned response IDs are separate facts. `ParsedSseBlock.is_local` identifies generated events; `response_id_is_local` excludes a generated ID even when SDK normalization wraps a real upstream error. These flags remain outside serialized event bytes and survive parsed-payload reattachment. Thus an oversized-frame failure cannot invent an upstream owner, while a real upstream error with a locally assigned ID remains a valid event for timing. The shared HTTP/direct/routed WebSocket normalizer and this provenance contract are owned here; HTTP timing consumes them through the owner dependency. Existing durable-log behavior is unchanged. See the [ownership requirement](spec.md#requirement-observed-http-response-ids-publish-same-process-ownership-before-delivery).

Canonical background JSON acknowledgements with status `queued` or `in_progress` carry the same authoritative response identity as SSE lifecycle events. The lifecycle parser includes both, and the HTTP relay keeps queued events on the parsed path. For example, a two-account request receiving a queued acknowledgement can immediately route a same-process continuation to its known account while its originating log is pending. An in-progress event following token output has the same ownership behavior. The provider still decides whether unfinished work can be continued.

## Owner-forward SSE framing

The [owner-forward framing contract](spec.md#requirement-owner-forward-http-streams-preserve-sse-event-boundaries)
uses the canonical CR/LF separator detector while retaining the bridge's own
scheduler and request budget. For example, a created event ending in
`\r\n\r\n` followed by a completed event now reaches the origin as two events
before EOF. A trailing CR can dispatch immediately; the following LF, if any,
belongs to that ending rather than the next event.

Malformed UTF-8 decodes as U+FFFD, including final unterminated bytes; downstream
JSON validation still applies. Valid multi-byte UTF-8 split across chunks was
already buffered safely and remains intact. Ordinary owners emit LF, making
this compatibility hardening for the receiver. No migration, operator setting,
account-selection change, or new event-size policy is involved.

## Detached retirement sweep deadline

Issue #2149 bounds aggregate detached-session lock waiting during request finalization. A sweep shares five seconds: if its first attempt consumes three seconds, the next receives two, and later attempts stop at expiry. Deferred generations remain tracked for later requests and their lifecycle owners. The deadline does not cancel resource-close owners or replace their existing close timeout.

## Routed file transport failover

File operations follow the existing pre-visible unary retry policy. The file client carries typed dispatch provenance through its service adapter so a refused account-proxy connection can retry another eligible account even though credential-safe transport messages omit low-level details. Typed replay eligibility takes precedence over message text: a proxy endpoint named `timeout-primary` must not turn a TLS verification failure into a retry.

For example, an unpinned upload whose account A cannot connect to its proxy can complete through account B, and the resulting file owner pin belongs to B. Finalization of a file pinned to A remains on A. Ambiguous request delivery, body-read failures, and host-wide network failures do not permit cross-account retries.

Finalization may issue several upstream polls within one downstream call. A refused connection on its first poll can still use an eligible fallback account. After a poll has returned `retry` from A, however, a later refused connection must fail the operation on A: the pre-dispatch status of the later request does not undo the earlier poll's account-local progress. This applies even when no file owner pin was found; direct transport polling retains its existing behavior.

## Scheduled retry cleanup races

Scheduled cleanup compares the selected observation timestamp, admission generation, failure count and null-safe failure detail before deletion. For example, an old row at generation 3 survives if a replay claims generation 4 after cleanup selected it, even when the timestamp still meets the age cutoff. A conditional-delete miss ends that cleanup pass after its selected batch; the next scheduled pass applies the normal retention rules to the surviving state. See the scheduled cleanup requirement in [spec.md](spec.md).

This uses existing columns and does not establish leased-receipt lifetime protection. The receipt lifecycle and stranded-owner and success-versus-claim policy decisions remain separate in #2271 and PR #1954.

## Health persistence after a terminal response

A failed account-error health write must not replace a Responses terminal that the client already received. For example, a quota failure followed by a database error must retain the original quota terminal and log the health failure. The retry loop contains ordinary post-terminal health exceptions after reservation settlement; it preserves cancellation and pre-terminal retry decisions. Issue #2033 has both a public `/v1/responses` regression and stream-handler coverage for keyed owner continuations. The implementation and excluded pre-terminal setups are recorded in `openspec/changes/archive/2026-09-10-fix-post-terminal-health-failures/context.md`.

## Ready transcript backlog

The background spooler continues bounded passes while eligible events remain, giving each operation one batch per pass. For example, 320 events queued before the first pass at batch size 32 require ten writes, without nine extra flush-interval waits. This changes backlog scheduling; paced-event coalescing and terminal persistence retain their existing behavior. Isolated database measurements and limits are recorded in `openspec/changes/archive/2026-09-10-drain-ready-http-bridge-spool/verification.md`.

## Native WebSocket receive bursts

The decoded native WebSocket receive queue shares byte accounting with the
existing helper event queue. A 4096-event cap permits ordinary small-frame
bursts; a 32 MiB accounted-payload cap bounds stalled consumers. Interpreted
Responses frames retain text and a decoded representation, so both count.
Overflow is a local consumer condition, not evidence of an unhealthy account.

For example, a 256-frame reasoning burst can remain queued while a send
acknowledgement completes. Sustained overflow still terminates that connection
without stopping the shared helper reader or unrelated connections. No automatic
replay is added because a dispatched request may already have had effects.

## Native Codex HTTP history is never anchored

Codex sends its complete local history on every HTTP Responses request. The
HTTP bridge forwards it over an upstream WebSocket and used to add its own
`previous_response_id` so that only the new items travel. That anchor is an
optimization: upstream keeps a `store=false` response only for the socket that
produced it. After a clean close (1000) or a service restart (1012) the
response is gone, even though the bridge's durable row still names it, and
the anchored request failed with `previous_response_not_found`.

The bridge therefore never adds an anchor to native Codex history. It sets the
same durable-anchor suppression a quarantine sets for full-resend payloads,
for every native history, so no injection point (fresh reattach, hydration of
the durable anchor, session-level, owner-forward recovery) adds one, and the
client's input goes upstream unchanged and untrimmed. The account and prompt
cache key are unchanged, so upstream prompt caching still matches the prefix;
the cost is request size only.

For example: a tool turn completes on socket 1, upstream closes socket 1, and
Codex sends the full history with the tool output. The bridge sends that
history once, without an anchor, on a new socket of the account that served
the thread, with the same prompt cache key. The next turn is again the
complete unanchored history on that socket and account. (A socket-bound
variant anchored that next turn to the new socket's response; the current
rule does not.)

The decision is protocol-based. The native decision is the route's: a native
Codex `User-Agent` or `originator` without an explicit OpenAI SDK marker, no
client `previous_response_id`, no `conversation`, a non-empty input list.
Proof that an anchor would be safe needs the producing socket, the response
the client continued from (duplicate requests leave siblings on one socket)
and its pending calls; tracking that leaked into replay, retry and
authentication paths, so the rule needs no proof at all. A client that
presents native identity but sends only a delta without an anchor is outside
the Codex HTTP contract; its body is forwarded as written and skips the
delta-only poison fail-closed. Other clients may send only a delta and rely on
the durable anchor, so they keep it; a client-supplied `previous_response_id`
is never dropped by this rule.

Everything else is unchanged. A verified full resend still ignores a stale
broad session owner and the durable owner stays the preferred account. A
socket that drops with no events is an ordinary transport failure of an
unanchored request, which the bridge already replays with the same body, and
an unanchored native request records no recovery checkpoint that could fence
the client's identical retry. Capacity retries, the authentication retry and
permanent-failure marking behave as for any other request. A durable owner
from a previous process is not excluded from selection: after a restart only
the socket is gone.

The anchored form also carried protections that the unanchored body has to
keep explicitly, so the request state marks native history:

- A close before any response event is replayed on the thread's owner. An
  id-free history looks account-neutral, and without a turn-state header the
  replay used to treat it as a fresh request and move the same prompt cache
  key to another account.
- An open hard-key retry circuit admits it, like the proof-gated full resend
  its anchored form was.
- When the bridge connect fails before dispatch for a thread bound to its
  durable owner, the request is not replayed over raw HTTP: that path cannot
  require the owner and would send the owner's encrypted reasoning elsewhere.
  The client receives the failure, as it did when the bridge had prepared an
  anchor.
- A recovery checkpoint does not fence it. A checkpoint written by an older
  anchored attempt never settles once its session is gone, and it used to
  refuse every identical retry until the body changed.

## Anchored local rebind

A request that carries an anchor and repeats the anchored turn's input is
trimmed: the anchor carries the stored prefix, so only the rest goes upstream.
When upstream rejects the anchor before `response.created` and the request has
no proof that it can go upstream without the anchor, the bridge rebinds to a
fresh session and retries with the same anchor, because the rejection can mean
the old session was not the anchor's owner. The retry used to rebuild its body
from the untrimmed request and re-run the interrupted-output injection against
the failed session. Upstream then received the stored prefix twice, once
through the anchor and once inline, and a sibling request that completed in
between could make the re-run injection miss an output. The retry now resends
the dispatched request and keeps its stored context, so the next turn still
trims against the full client input. Its anchor is the one the dispatch was
sent with: the submit's operation lookup can advance a request to a response
another worker or a sibling request completed, and the retry acquires its
session, records its operation and writes its frame with that same anchor.

For example, a client sends `[U1]`, then `[U1, U2]` with
`previous_response_id` of the first response. The bridge sends `[U2]` with the
anchor; after a rejection and a local rebind, the retry also sends `[U2]`, not
`[U1, U2]`.

## WebSocket model rejections of unanchored bodies

Upstream rejects a replayed tool call's `namespace`, so the wire text drops it
while the local request keeps it. On the text alone a namespaced history looks
account-neutral, although it belongs to the account that produced it. The
direct WebSocket path records the decision from the client's request while the
namespace is still present (leaving out `stream` and `background`, which the
`response.create` frame never carries). A pre-acceptance model rejection may
move an unanchored request only when the wire text is account-neutral and that
recorded decision is not account-bound: the recorded value only vetoes, so it
never makes a body portable that its text would keep. Other owner-switch paths
(selection and dispatch binding, the authentication retry) still judge the
text.

For example: a Codex WebSocket session without a turn-state owner sends its
full history with a `collaboration` `spawn_agent` call and its output, and
account A answers `model_not_found` before `response.created`. The client
receives A's rejection; the history is not re-sent to B with the namespace
stripped. The same history without the namespace still moves once to B.

The connect loop computes the owner requirement before selection, and
selection then binds an account-scoped body (a captured Codex history carries
item ids) to the account it picks. A handshake model rejection from that
account, or from an account whose recorded decision keeps the body, surfaces
its original error. Every other handshake failure keeps the requirement
computed before selection and its ordinary failover decision.

Known limitations, unchanged from upstream:

- For a selection-bound body, a handshake 401, 403, 429, quota or 5xx
  failure, a confirmed proxy pre-dispatch failure and a deleted account all
  exclude the account; the next selection still requires it and ends in
  `previous_response_owner_unavailable`.
- Only the model-rejection paths consult the recorded decision. On every
  other path (those handshake failures, the accepted output-free capacity
  replay, the accepted transport-close replay, the authentication replay) a
  text-neutral namespaced history still moves as before.

Dropping `stream` and `background` at recording also applies to the fresh
body retained behind a proxy-injected anchor. That is benign: the frame
never carries either field, so the decision now describes what is sent.

## Reasoning policy for configuration_update items

Mid-conversation effort changes arrive as `configuration_update` input items, so a policy that reads only the request-level `reasoning.effort` and model aliases lets an item select any effort. Clients persist the items in the history they resend, so a policy that rejects a thread for one historical item would break it for good: a user who toggles effort under a pinned key, or a thread that recorded an item before this policy existed, would fail on every later turn, and compaction could not recover it. See the configuration_update policy requirement in [spec.md](spec.md).

A pinned key therefore rewrites the items, just as it already replaces the request-level effort. Both are taken from the same outgoing body for each destination, so they stay equal: with `enforcedReasoningEffort: "minimal"` a subscription model whose lowest effort is `medium` receives `medium` in both, while a model source receives `minimal`; with `enforcedModel: "gpt-5.1-xhigh"` a subscription receives `high` and a source `xhigh`. For an unchanged key policy, model catalog and source configuration the rewrite is deterministic, so the prompt prefix is identical on every turn; the `minimal` fallback reads the live model registry, so a catalog change moves it. A source `reasoning` override that sets only `summary` keeps the pinned effort, while one that sets `effort` wins for both the request and the items; an explicit `null` sets the items' effort to `null` too, mirroring the request-level field. A thread whose earlier turns were forwarded with unrewritten items changes its prefix once. Items already held upstream behind a client-supplied `previous_response_id` are outside the request and are not rewritten; this is a known limit.

An allowlist key without a pin judges only the effective item, the last one with a usable effort, because an overridden item no longer selects anything. For example, with an allowlist of `low` and `high`, `max` followed by `high` passes and is forwarded unchanged, while `low` followed by `max` is rejected with `param` `input[3].reasoning.effort`. The index counts positions in the forwarded input, after system and developer messages are lifted into `instructions`, so it can be lower than the client's own index. This is safe only because compact trimming keeps the effective item as a required anchor; without that, trimming could drop the later item and revive `max`, which also silently changed effort for clients without an API key. Only that item is anchored: anchoring every update split the history into one gap per item, each priced as an omission marker, and a history of 1,100 alternating updates and short messages was rejected as too large.

The request-level effort is still judged on every turn. An allowlist key whose request-level starting effort is later removed from the allowlist keeps failing for that thread until the client changes its request-level effort; a later `configuration_update` item does not excuse it.

This covers policy enforcement only. Request logs, accounting, TTFT cohorts and the request-level effort sent to model sources for keys without a pin still ignore the items; that remains open in issue 2664.

## Keyless thread identity on the upstream-main hoist view

The derived thread identity scopes its item digests by API key, model class
and `instructions`, and matches a trailing window of 32 items. Upstream `main`
hoisted every `system`/`developer` message into `instructions`, so that text
was in the scope and never in the window. JSON mode now keeps its instruction
in `input` (upstream counts the word "json" only in input messages), and
`developer` messages stay in `input`. Computing the identity on the wire
shape broke it three ways: two threads that differed only in that message and
ended in the same 32 items shared a key and an account; a client repeating a
fixed developer message at the head of a sliding window minted a new key on
every trim (four trims, four keys), because that message sat at the front of
every window; and partial rules (only the leading run in the scope) moved a
mid-thread reminder into the scope as soon as a trim made it the first item.

Rather than invent a rule, the anchor, the inferred HTTP bridge key and its
history check are computed on the request as upstream `main`'s hoist produced
it (`upstream_hoist_view`): the top-level `instructions` followed by the text
of the developer messages still in `input`, in wire order, and the items
without those messages. The view is built from the request as it is; the only
recorded input is the Lite classification of the request as the client sent
it, because a warm session trims the Lite tool bundle off later frames and
the suffix would otherwise be judged as non-Lite, dropping a Plan or Default
mode message from the window. The history check uses the same items as the
hash: upstream hoisted the text of a JSON-mode developer message with an image
and left the image as a `user` item, so such a follow-up is history. For the
captured Codex bodies with their key removed, and for keyless bodies with JSON
mode, a developer image part, mid-thread developer messages and compact
requests, the scope, window digests and bridge key equal those of upstream
`main`.

Known limitation (keyless clients only): `system` messages are hoisted at
validation, so the view cannot recover their position relative to developer
messages and always puts the system text first. Upstream joined them in input
order, so two keyless threads that interleave `system` and `developer`
messages and differ only in their order share a scope here where upstream
split them; they still need identical recent items to share a key.

Known limitation (keyless clients only): developer text that validation
discards, a `developer` message whose content is blank or only reasoning
parts, is gone before the view is built, so it does not change the derived
key. Upstream hoisted that text before sanitising and moved the key. The
fork's behaviour is intended and pinned by a compatibility test: the message
never reaches upstream, so it cannot change the prompt prefix either.

Known limitation (keyless non-Lite WebSocket clients only): on the direct
WebSocket path the handler injects `previous_response_id` and trims the stored
prefix before the affinity resolver runs, so the view sees only the trimmed
request. Two threads of one API key that differ only in their leading
developer messages and share the trimmed window can derive the same key,
where upstream kept them apart. This over-shares within one API key and does
not churn keys per turn; keys never cross API keys, and clients that send
their own `prompt_cache_key` (Codex) are unaffected.

Known limitation (keyless clients only; Codex sends its own
`prompt_cache_key`): a changed or added instruction message changes the
scope and mints a new key and bridge locality. For a refreshed leading
message (a skills list) the upstream prefix is cold anyway; for a reminder
added mid-thread the prefix up to it is still warm on the old account, so the
thread can lose its cache once. That is upstream `main`'s behaviour and is
kept.
