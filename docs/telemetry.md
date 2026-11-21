# Anonymous telemetry

**Off by default since the fork's `fork.2` release.** The telemetry endpoint,
`https://telemetry.tokmaxxing.com`, is a collector operated by the upstream Soju06/codex-lb
project. This fork does not operate it and cannot see what it receives, so with no
configuration a fork instance from `fork.2` on sends nothing and opens no connection to it.

The first fork release (`fork.1`) still has upstream's default: with no saved decision it
sends telemetry and shows the consent dialog, unless `CODEX_LB_TELEMETRY_ENABLED=false` is set
before a decision is saved. Upgrade, or set that variable.

When an operator opts in, codex-lb sends an anonymous usage snapshot to that upstream
collector when the service starts and every 24 hours. In a multi-replica deployment, only the
elected leader builds and sends the snapshot.

## What is sent

Settings shows the exact JSON envelope the instance would send. While telemetry is off, that
preview uses a throwaway `instance_id` and stores nothing. The signed snapshot body has
three fields:

```json
{
  "instance_id": "<random UUIDv4>",
  "metrics": { "<schema below>": "..." },
  "timestamp": "<current UTC time>"
}
```

The versioned `metrics` schema contains only these fields:

- `schema_version`, active `consent` (`undecided` or `enabled`), random `instance_id`, codex-lb
  `version`, Python version, OS, architecture, and process uptime
- `deploy`: deployment method, database backend and size bucket, replica count, and whether
  trusted reverse-proxy headers are enabled
- `accounts`: bucketed pool and plan counts, whether workspace accounts exist, routing policy,
  limit warmup, and whether an egress proxy is used
- `usage_7d`: request/success/token aggregates, bucketed cost, request-kind and transport/service
  tier shares, allowlisted client families and model names, bucketed output-token averages,
  latency percentiles, rate-limit ratio, and allowlisted upstream error codes
- `features`: booleans for optional features plus bucketed API-key count and model-source count

Registration sends `app_name`, `app_version`, `deployment_mode`, an intentionally empty
`environment`, the random `instance_id`, coarse `os_arch`, and the Ed25519 `public_key` used to
verify signed updates. Activation sends only `{"action": "activate"}`.

The schema never includes account emails, workspace identifiers, client IP addresses, API keys,
request or response content, raw user-agent strings, per-account records, custom model names, or
free-text errors. Exact schemas and privacy constraints live in the
[telemetry OpenSpec capability](https://github.com/insciqq/codex-lb/tree/main/openspec/specs/telemetry).

## Consent, opting in and disabling

From `fork.2` on, `CODEX_LB_TELEMETRY_ENABLED` defaults to `false`. With no saved dashboard
decision, an instance behaves exactly as if that value were set: telemetry is disabled, no
snapshot or opt-out notice is sent, and the dashboard shows no consent dialog. Upstream
releases instead default to informed opt-out consent with a one-time dialog.

To opt in, either enable telemetry in Settings, which saves the decision, or set:

```bash
CODEX_LB_TELEMETRY_ENABLED=true
```

Either way, snapshots go to the upstream project's collector, not to this fork. To keep
telemetry off explicitly, set `CODEX_LB_TELEMETRY_ENABLED=false` or disable it in Settings.

The environment value applies only while no dashboard decision has been saved; a saved
dashboard decision always takes precedence over it. The Settings toggle shows a notice while
the environment value is the one in effect. When telemetry resolves to disabled, codex-lb opens
no connection to the telemetry endpoint. The environment opt-out is always completely silent.

When a dashboard decision changes telemetry from active to inactive, codex-lb makes one final
signed request to `POST /v1/optout` so aggregate opt-out counts remain accurate. If the instance
has not contacted the collector in this process, it first performs the normal registration and
activation. Re-enabling and later disabling from the dashboard sends one new notice for that new
transition. Repeating an already-disabled decision sends nothing.

The opt-out request uses the same `X-Instance-ID` and Ed25519 `X-Signature` headers as a snapshot.
Its canonical JSON body is:

```json
{
  "app_version": "<codex-lb version>",
  "event": "optout",
  "instance_id": "<random UUIDv4>",
  "occurred_at": "<current ISO 8601 UTC time>"
}
```

This single decision-time notice is the only exception to disabled telemetry silence. It is
sent only for a dashboard-driven active-to-inactive transition; setting
`CODEX_LB_TELEMETRY_ENABLED=false` never sends it.

## Retention and failures

Each snapshot summarizes the previous seven days of data already present in `request_logs`.
codex-lb does not keep a separate local telemetry history and does not queue a failed send. The
collector's server-side retention duration is not currently specified; assume transmitted
snapshots remain stored until a published retention policy or explicit deletion.

Snapshot and opt-out endpoint failures use a five-second total timeout, retry no more than once,
are logged only at debug level, and never interrupt proxy traffic or change the dashboard
settings response.

*Source of truth: [telemetry OpenSpec capability](https://github.com/insciqq/codex-lb/tree/main/openspec/specs/telemetry)*
