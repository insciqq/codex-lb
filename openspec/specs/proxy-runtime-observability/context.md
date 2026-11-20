# Proxy Runtime Observability Context

## Purpose and Scope

This capability defines what operators should be able to see in the live server console while debugging proxy traffic.

See `openspec/specs/proxy-runtime-observability/spec.md` for normative requirements.

## Decisions

- **Timestamps are always on:** timestamped console logs are a baseline operator need, not a debug-only feature.
- **Request tracing is opt-in:** outbound request summary and payload tracing remain configurable because payload logs can be noisy or sensitive. Since issue #1340 phase 1 the switch is the single `CODEX_LB_TRACE` comma-separated channel list (`shape`, `shape_raw_cache_key`, `payload`, `service_tier`, `upstream_summary`, `upstream_payload`); empty default = all off. It is an incident-debugging knob for interactive use only.
- **Error logs must be correlated:** request id, endpoint, status, code, and message are the minimum useful fields for debugging 4xx/5xx failures.
- **Prewarm observability is outcome-only:** the Codex HTTP-bridge prewarm canary experiment finished, so its bucket/cohort dimensions were retired (issue #1340 phase 4). The `codex_lb_http_bridge_prewarm_total` counter is labelled by `outcome` only, request logs record `prewarm_status` / `prewarm_latency_ms` (statuses: `not_applicable`, `skipped`, `success`, `timeout`, `error` — `canary_miss` no longer occurs). The legacy `prewarm_canary_bucket` / `prewarm_eligible_reason` request-log columns stayed declared but unwritten through v1.22–v1.24; `retire-prewarm-canary-column-mappings` removed them from the ORM model and allow-listed the retained physical columns in the schema-drift gate, because a previous-release replica still maps them and renders explicit NULLs in its INSERTs while the migration Job runs ahead of the workload roll. The Alembic drop revision ships the release after (see the next-release queue in `openspec/specs/deployment-installation/context.md`).
- **TTFT datasource selection stays in Grafana:** the Helm chart packages the
  TTFT dashboard but does not provision a PostgreSQL datasource or its
  credentials. The visible, single-select `DS_SQL` variable keeps
  installation-specific datasource UIDs out of chart values while routing all
  four SQL panels through one explicit selection.

- **Account inventory gauge reuses existing reads (issue 2426):** `codex_lb_accounts_total{status}` was declared but never set. It is now set from three full account listings that already exist, so it needs no extra query:
  - the routing-availability snapshot refresh;
  - the load balancer's selection-input account read;
  - the scheduler leader's listing at the start of each usage-refresh tick.

  The routing refresh alone would miss most status writes: rate-limit, quota and reauthentication writes, background cooldown recovery, and balancer marks made after the selection read do not bump `account_routing`. Freshness differs by instance:
  - On the scheduler leader, an out-of-band write is reported within one usage-refresh tick (the gap between tick starts is at most the usage refresh interval, 60 s by default, plus the previous tick's own work), even with no traffic, including in a single-instance deployment.
  - A non-leader replica runs no usage-refresh ticks. It reports such writes only on its own next selection rebuild (when it serves traffic) or its next `account_routing` refresh, so an idle non-leader can lag. Read the leader's series, or use `max by (status)` as the bundled dashboard does, rather than any single idle replica.

  A scrape-time database refresh was rejected: the scrape server lives in the application lifespan, and a per-scrape refresh would race the routing snapshot. The routing refresh publishes before its awaited session close, so an older refresh that finishes late cannot overwrite newer counts. Metric errors are logged and never abort the refresh.

  Multiprocess mode uses `livemostrecent` instead of `liveall`. `liveall` exported one series per worker PID for the same shared table. `livemostrecent` keeps the newest write per status label; that is not an atomic snapshot across labels. `mark_process_dead` removes dead workers' files. Several workers sharing one metrics directory is outside the supported topology; the mode only keeps such a setup from duplicating the inventory.

  An availability gauge (routing-eligible accounts) is a separate feature and is not part of this fix.

## Operational Notes

- Use request ids to correlate inbound proxy logs, outbound upstream traces, and client-visible failures.
- Prefer summary tracing in normal debugging sessions; enable payload tracing only when the exact normalized outbound request matters.
- For direct compact `5xx` failures, look for `proxy_compact_failure` alongside `upstream_request_complete`; together they show the compact failure phase, failure detail, exception type, retry metadata, and affinity source.
- After the Grafana sidecar imports the TTFT dashboard, select the ordinary
  PostgreSQL datasource that points to the codex-lb database from the visible
  **PostgreSQL** dropdown. A datasource registered only as a frontend runtime
  plugin is not listed by Grafana's datasource variable.
- Timeout invariant violation logs describe startup `Settings` and imported
  constant validation only. They intentionally avoid request-scoped overrides,
  runtime-derived effective timeout values, payloads, API keys, access tokens,
  raw affinity keys, account emails, and other high-cardinality identifiers.

## Affinity decisions in request logs

Issue #2349 adds three nullable columns to existing request logs: `sticky_key_source`, `sticky_kind`, and `sticky_key_hash`. They work without trace settings. Callers with `conversations:read` permission can also read them as `stickyKeySource`, `stickyKind`, and `stickyKeyHash` in `GET /api/request-logs`. Responses without that permission hide all three fields through the existing sensitive-metadata gate.

The hash is the first 16 lowercase hexadecimal characters of SHA-256 over the resolved selection key encoded as UTF-8. For example, a resolved key of `abc` records `ba7816bf8f01cfea`. Session selection keys can differ from raw session headers, so hashing the header separately does not reproduce that value. The metadata never stores raw keys or prompts, even when raw-key tracing is enabled. A hash supports equality grouping, but it is not protection against guessing low-entropy keys. Existing request-log retention applies.

Direct streaming rows retain their existing attempt granularity. Compact records its final operation, while native WebSocket and HTTP bridge rows describe the settled or failed request state. A row covering several sends records its final policy, not an intermediate decision history. Existing recovery can clear a key; that produces a null hash while retaining the original source classification. Metadata alone does not explain hard-owner precedence, rerouting, or account-health decisions.

Rows before affinity resolution, historical rows, auxiliary control/file/transcription/realtime/warmup rows, and external-model-source rows can have all three fields null. This means observation was unavailable. An explicit no-affinity observation uses source `none`; its kind and hash are null. No historical decision is reconstructed.

For a bounded administrator query:

```sql
SELECT sticky_key_source, sticky_kind, sticky_key_hash, COUNT(*) AS rows
FROM request_logs
WHERE requested_at >= CURRENT_TIMESTAMP - INTERVAL '1 day'
GROUP BY sticky_key_source, sticky_kind, sticky_key_hash;
```

## Authentication migration convergence

The affinity history converges with dashboard roles, users, the final compatibility-credential projection and audit actor columns through a no-op merge. Published revisions remain unchanged. Databases already on the authentication branch retain their current credentials and session generations; the earlier credential projection is not replayed on merge-only reupgrade. Databases on the older affinity history run the existing authentication backfills once. Ledgerless schema bootstrap retains those migrations' existing legacy-credential projection behavior.

The subsequent invite migration converges through a second no-op join. Pending, consumed and revoked invite rows retain their hashes, expiry/consumption/revocation times, creator snapshots and flags. The older affinity history creates an empty invite table through the unchanged upstream migration.

## Optional model-source telemetry

Source metadata is ancillary to the forwarded response. Preserve reported reasoning
usage, but leave missing reasoning unknown; reject boolean counts and values beyond
PostgreSQL's signed 32-bit request-log storage range. Validate timing operands and
their sum before rounding. For example, two individually finite 1e308 timings are
not usable measurements and cannot interrupt an otherwise valid response.

An invalid count is not the same as a missing one. Chat streams send
`"usage": null` on content chunks, and those keep the usage seen so far; a frame
that reports `{"prompt_tokens": true, "completion_tokens": 180}` replaces it with
"unavailable", so a limited API key fails closed instead of being charged the one
output token an earlier frame reported. A frame with `{"completion_tokens": 180}`
alone is treated the same way, and a later complete pair replaces it. Likewise a
transcription reporting `{"output_tokens": 180, "total_tokens": 184}` or an
invalid split count is not re-read through the total-only fallback as 184 input
tokens. Reasoning
tokens are part of the output and are capped at the output count, as cached
tokens are capped at the input count.

The usage parser incrementally decodes UTF-8 and waits for complete SSE events,
including multi-line data and CRLF split across network chunks. Forwarding still
yields original upstream bytes. Source routing, metric qualification and report
cohorts are unchanged. See the optional-source-metrics requirement in spec.md and
`openspec/changes/archive/2026-09-28-harden-model-source-usage/verification.md`.
