# bridge-ring-membership Specification

## Purpose
Governs how replicas join, stay in, and leave the shared bridge ring that routes hard-affinity HTTP bridge requests to the owning replica. Sibling replicas must converge on the same view of which instances are alive, so a replica registers before serving bridge traffic, heartbeats on a fixed cadence, ages its row on shutdown rather than deleting it, and dead rows are eventually purged.
## Requirements
### Requirement: Replicas register in the bridge ring before serving bridge traffic
Each replica MUST register its instance id (and advertised endpoint when configured) in the shared `bridge_ring_members` table before hard-affinity HTTP bridge requests are admitted. While registration is incomplete in a multi-replica deployment, hard-affinity bridge requests MUST wait for registration up to the configured connect timeout and fail with a retryable `bridge_owner_unreachable` error when the wait expires.

#### Scenario: Hard-affinity request before registration completes
- **WHEN** a hard-affinity HTTP bridge request arrives on a replica whose ring registration has not completed
- **AND** the deployment requires cluster registration (multi-instance ring or non-loopback advertise URL)
- **THEN** the request waits for registration up to the configured connect timeout
- **AND** it fails with a retryable `bridge_owner_unreachable` error if registration is still incomplete

### Requirement: Ring membership is maintained by periodic heartbeats
Each registered replica MUST refresh its ring row via an upsert heartbeat every 10 seconds so sibling replicas observing the shared table converge on the same active-member view. Ring readers MUST treat a member as active only when its heartbeat is within the 30-second stale threshold.

#### Scenario: Missed heartbeats age a member out of the active ring
- **WHEN** a replica stops heartbeating for longer than the stale threshold
- **THEN** ring readers no longer include that instance in the active member list
- **AND** owner-endpoint resolution for that instance returns no endpoint

#### Scenario: Heartbeat recovers a row removed by a sibling
- **WHEN** a replica's ring row was deleted or aged by another process
- **THEN** the replica's next heartbeat re-upserts the row with a fresh timestamp

### Requirement: Ring heartbeat is independent of bridge maintenance
The per-replica bridge maintenance passes (durable ownership reconciliation, stale operation abandonment, idle session sweep) and the cap-partition refresh, including the first refresh after registration, MUST run in a loop separate from the ring heartbeat, on the heartbeat cadence, so a slow or blocked maintenance pass cannot delay the next heartbeat. Both loops MUST start as soon as registration completes. The maintenance loop MUST run one pass at a time: a pass that has not finished MUST NOT have another pass started alongside it. A failing maintenance pass or cap-partition refresh MUST be logged and MUST NOT stop either loop.

The replica's ring registration, heartbeat and stale-marking writes MUST use a dedicated unpooled (`NullPool`) engine on the main database URL that no request, background-task or maintenance work shares, so work holding or waiting for pooled connections cannot starve the heartbeat. Each ring write opens one transient connection and nothing is held between writes. The cap-partition refresh is maintenance and MUST read membership through the main pool, so the ring writes add at most one transient connection per replica. The engine MUST be disposed at shutdown after stale-marking or after stale-marking is skipped. In-memory SQLite, where a second engine would open a separate empty database, uses the main session factory. No setting is introduced for this.

#### Scenario: Blocked maintenance pass does not age the ring row
- **WHEN** a maintenance pass blocks without returning
- **THEN** the replica keeps heartbeating its ring row every heartbeat interval
- **AND** no second maintenance pass starts while the blocked pass is still running

#### Scenario: Failing first cap-partition refresh does not stop the heartbeat
- **WHEN** the cap-partition refresh right after registration stalls or raises
- **THEN** the replica still heartbeats its ring row every heartbeat interval
- **AND** a raised refresh is logged and retried on the next maintenance pass

#### Scenario: Ring writes do not share any pool
- **WHEN** the replica writes its ring row
- **THEN** the session is bound to the dedicated unpooled ring engine, not to the main or background pooled engine
- **AND** that engine is disposed after the shutdown stale-marking step

#### Scenario: Shutdown stops both loops before aging the ring row
- **WHEN** a replica shuts down gracefully while a maintenance pass is in progress
- **THEN** the heartbeat loop and the maintenance loop are both cancelled and awaited before the ring row is aged
- **AND** no heartbeat is written after the ring row is aged

#### Scenario: Ring loops that outlive the stop bound do not delay shutdown
- **WHEN** cancellation-deferring cleanup keeps either loop running for longer than the 2-second stop bound
- **THEN** shutdown proceeds after the bound without aging the ring row, so the row expires after the stale threshold
- **AND** the shutdown is not recorded as clean, because database work is still unwinding

### Requirement: Shutdown ages the ring row instead of deleting it
On graceful shutdown a replica MUST age its ring row's heartbeat close to the stale threshold rather than deleting the row, leaving a short grace window (heartbeat interval plus 5 seconds) during which sibling workers sharing the same instance id can refresh the row while a fully terminating pod still ages out quickly.

#### Scenario: Terminating pod leaves a short grace window
- **WHEN** a replica shuts down gracefully
- **THEN** its ring row's heartbeat is set so the member ages out after the shutdown grace window
- **AND** the row is not deleted, so a surviving sibling worker's next heartbeat can restore it

### Requirement: Dead ring rows are purged
The background cleanup loop MUST delete `bridge_ring_members` rows whose heartbeat is older than 24 hours so rows for permanently departed replicas do not accumulate. Rows within the retention window MUST NOT be deleted so shutdown stale-aging and restart recovery keep working.

#### Scenario: Cleanup removes long-dead members and keeps recent ones
- **WHEN** the cleanup loop runs
- **AND** one ring row's heartbeat is older than 24 hours while another's is recent
- **THEN** the old row is deleted
- **AND** the recent row is preserved

