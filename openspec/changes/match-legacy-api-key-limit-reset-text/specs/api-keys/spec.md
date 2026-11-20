## ADDED Requirements

### Requirement: Limit guards compare reset deadlines by exact value

Guarded API-key limit updates (expired-limit reset, usage reservation, reservation adjustment and manual reset) MUST match a stored `reset_at` exactly when it equals the expected deadline, including SQLite rows that spell it in a legacy text form: a `T` separator, trailing fractional zeros trimmed down to no fraction, or no seconds when they are zero. They MUST NOT match a stored deadline that differs from the expected one, even below the millisecond. Each guarded update MUST remain a primary-key lookup.

#### Scenario: Legacy spelling of the same deadline
- **GIVEN** a SQLite limit row whose `reset_at` text is `2030-01-02T03:04:05.123456`
- **WHEN** a usage reservation names the deadline `2030-01-02 03:04:05.123456`
- **THEN** the reservation succeeds

#### Scenario: A deadline that differs below the millisecond
- **GIVEN** a SQLite limit row whose `reset_at` text is `2030-01-02 03:04:05.123400`
- **WHEN** a reservation, adjustment or manual reset names the deadline `2030-01-02 03:04:05.123000`
- **THEN** the guarded update matches no row and the limit is unchanged

#### Scenario: Legacy second-precision row expires
- **GIVEN** a SQLite limit row whose `reset_at` text is `2026-09-19 10:30:18`
- **WHEN** the expired-limit sweep runs after that deadline
- **THEN** the row's usage resets to 0 and its next deadline is stored in canonical form

## MODIFIED Requirements

### Requirement: Background limit reset sweep makes forward progress

The background expired-limit sweep MUST attempt each selected limit at most once per invocation, including when the guarded reset update matches no row. It MUST page through due limits in `(reset_at, id)` order on the `reset_at` index, advance past every selected row by its stored sort key, and finish for a finite set of limits without selecting a skipped row again. It MUST guard each reset with a compare-and-set on the exact `reset_at` it selected, MUST NOT attempt a limit again in the same invocation after its guarded update missed, commit each processed batch, and count only successful resets. Limits with `reset_at >= now` MUST remain unchanged in every stored spelling. On SQLite, a limit whose legacy `T`-separated `reset_at` text falls earlier on the current day MUST be treated as due.

#### Scenario: A limit changes between selection and its guarded update

- **GIVEN** the sweep selected an expired limit and another writer then changed its `reset_at`
- **WHEN** the guarded reset update for that limit runs
- **THEN** it matches no row, the sweep does not count or modify the limit, and it does not select it again in this invocation
- **AND** later expired limits are still reset

#### Scenario: A batch of guarded misses precedes resettable limits

- **GIVEN** an entire batch of expired limits fails its guarded updates and later expired limits are resettable
- **WHEN** the background sweep runs
- **THEN** it proceeds beyond the skipped batch and resets the later limits
- **AND** it returns only the number of successful resets and leaves unexpired limits unchanged

#### Scenario: Nothing is due

- **GIVEN** no limit has `reset_at < now`
- **WHEN** the background sweep runs
- **THEN** it reads only the `reset_at` index range below `now` instead of scanning every limit

#### Scenario: A deadline equal to now

- **GIVEN** a SQLite limit stores `2026-10-19 12:00:00.1234` or `2026-10-19T12:00:00.1234`
- **WHEN** the background sweep runs at `2026-10-19 12:00:00.123400`
- **THEN** the limit is not reset

#### Scenario: A guarded miss moves to a later due deadline

- **GIVEN** the sweep selected an expired limit and another writer moved it to a later deadline that is still before `now`
- **WHEN** the sweep reaches that limit again in the same invocation
- **THEN** it does not reset it

#### Scenario: A legacy same-day T spelling is due

- **GIVEN** a SQLite limit stores `2026-10-19T08:00:00` and the sweep runs at `2026-10-19 12:00:00`
- **WHEN** the background sweep runs
- **THEN** the limit is reset in that run
