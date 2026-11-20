## ADDED Requirements

### Requirement: Operation transcript retention bounds each write transaction

Operation transcript retention MUST delete an operation's transcript material
and its operation row in separate write transactions, each removing at most a
fixed number of transcript event rows or chunk rows. On SQLite, retention MUST
release the writer slot between these transactions. Retention eligibility and
the retention window MUST stay the same as for an unbounded purge.

#### Scenario: Large transcript is purged in bounded transactions

- **GIVEN** an expired terminal operation with more transcript rows than one
  retention transaction may delete
- **WHEN** retention purges it
- **THEN** every write transaction deletes at most the per-transaction row
  limit
- **AND** another writer can take the SQLite writer slot between them
- **AND** the operation row and all its transcript material are gone at the
  end

### Requirement: Bounded retention transactions recheck eligibility

Each bounded retention transaction MUST recheck the retention and owner/lease
predicate before deleting. Retention MUST stop purging an operation at the
first transaction in which it is no longer eligible and MUST leave its
operation row in place.

#### Scenario: Operation owned again during the purge keeps its row

- **GIVEN** a stale nonterminal operation of an ownerless session whose
  transcript retention has started to delete
- **WHEN** the session gets a live owner lease before the next retention
  transaction
- **THEN** retention deletes no further material of that operation
- **AND** the operation row remains

### Requirement: Bounded retention never deletes an operation row with transcript material

Retention MUST delete an operation row only when none of its transcript event
or chunk rows remain, so the operation delete never cascades into transcript
rows. Remaining material MUST be removed in further bounded transactions.

#### Scenario: Lease expires between retention transactions

- **GIVEN** a stale nonterminal operation whose session regains an owner after
  the first bounded transaction and whose lease expires before the next one
- **WHEN** retention continues
- **THEN** every remaining transcript row is removed in a transaction within
  the per-transaction row limit
- **AND** the transaction that deletes the operation row removes no transcript
  rows

### Requirement: Retention yields operations locked by the request path

On PostgreSQL, retention selection and its eligibility recheck MUST NOT wait
for a session or operation row that another transaction holds locked; they
MUST leave that operation to a later pass and let unlocked operations fill
the batch. Retention deletes MUST NOT use `RETURNING`.

#### Scenario: Duplicate request overlaps retention

- **GIVEN** a request that locked the owning session row and is about to lock
  the expired operation row
- **WHEN** retention reaches the same operation
- **THEN** both finish without a deadlock and retention deletes nothing of
  that operation in this pass

### Requirement: Scheduled retention enforces its time budget between transactions

A scheduled retention pass MUST check its time budget after every committed
retention transaction that deleted rows, stop when it is exhausted, and report
likely backlog. Every call MUST still make progress on the oldest selected
operation. A cancelled or failed pass, scheduled or at startup, MUST report
the operation deletes it already committed.

#### Scenario: Budget runs out inside a batch

- **WHEN** the pass budget is exhausted while an operation still has
  transcript rows
- **THEN** the pass stops after the current committed transaction with outcome
  `time_budget_exhausted` and likely backlog
- **AND** the next pass resumes the same operation

#### Scenario: Cancellation after a committed operation delete

- **WHEN** the pass is cancelled after an operation delete committed
- **THEN** the cancelled result counts that deleted operation

### Requirement: Retention paces the SQLite writer only on the scheduled path

On SQLite, scheduled retention MUST pause after each retention transaction for
as long as that transaction held the writer after it acquired the in-process
writer gate, capped at one second. Startup retention and PostgreSQL MUST NOT
pause.

#### Scenario: Transaction queued behind another writer

- **WHEN** a retention transaction waited for the writer gate before running
- **THEN** the pause after it does not include the waiting time
