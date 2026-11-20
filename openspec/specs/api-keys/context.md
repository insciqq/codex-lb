# API Keys Context

See `openspec/specs/api-keys/spec.md` for normative requirements.

## Background reset sweep progress

The sweep pages through due limits in `(reset_at, id)` order on the `reset_at`
index and keeps the stored sort key of the last selected row as its cursor.
It advances past every selected row, including guarded misses, so batches stay
bounded and a skipped row is reconsidered only on the next scheduled sweep.
With nothing due it reads only the index range below `now`.

On SQLite `reset_at` is text. Older binders wrote other spellings of the same
value, for example `2026-09-19 10:30:18`, `2026-09-19T10:30:18` or
`2030-01-02T03:04:05.123456`, which parse to the same datetime as the bound
`...SS.ffffff` form but never equal it as text. The guards therefore match the
stored text against every lossless spelling of the expected value (separator,
trailing fractional zeros, missing seconds). A value that differs, even below
the millisecond (`.123400` versus `.123000`), never matches. A `T`-spelled
deadline earlier today sorts after today's space spellings, so the SQLite sweep
also scans today's `T` range below `now`. The guarded write stores the
canonical form, so legacy rows heal on their next reset without a migration.

## Request-aware reservation estimate

The admission budget for token and `cost_usd` limits (Requirement
"Request-aware API-key usage reservations") sizes the input side from the
forwarded request payload: `min(utf8_length(serialized_payload_minus_caps), 8192)`.
Two implementation notes keep that value exact while avoiding redundant work on
the hot path:

- **Shared dump.** `ResponsesRequest.to_payload()` is deterministic, so the
  HTTP bridge prepare path computes it once and threads the same dict through
  client-metadata derivation, the forwarded `response.create` frame and
  `estimate_api_key_request_usage(payload, upstream_payload=...)`. The budget,
  frame bytes and input fingerprints are byte-identical to computing each stage
  from its own dump. When the prepare path rewrites the request (replayed
  side-effect tool-call dedupe under `previous_response_id`) the caller's dump
  is discarded and recomputed from the rewritten request, so the forwarded
  frame never carries un-deduped input. Callers that do not hold a dump keep
  the default single-argument form.
- **Early-exit serialization.** Because the estimate is capped at 8192 bytes,
  the estimator returns the cap as soon as it is proven: an `instructions`
  string of 8192+ characters alone suffices (a JSON string literal is never
  shorter than its character count), otherwise the payload is streamed through
  the same `sort_keys`/`ensure_ascii=False` encoder and stopped once 8192 bytes
  have been produced. Sub-cap payloads still yield the exact serialized length.
  The opaque-context checks (`previous_response_id`, `conversation`, file or
  image references) run before either shortcut, so the conservative `None`
  budget is unchanged.

Edge: a lone surrogate (`"\ud800"`) anywhere in the payload used to raise
`UnicodeEncodeError` (HTTP 500) from the full dump. It now raises only when it
sits in a chunk that is actually UTF-8 encoded, i.e. within the first ~8 KiB of
serialized output and not inside a string literal that the length shortcuts
(`instructions` >= 8192 chars, or a single chunk that alone covers the
remaining budget) prove the cap without encoding. Surrogates skipped that way
yield the 8192 cap like any other large payload.
