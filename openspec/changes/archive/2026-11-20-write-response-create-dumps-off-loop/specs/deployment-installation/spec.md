## MODIFIED Requirements

### Requirement: Response-create dump directory is bounded without configuration

The oversized response-create dump directory under `<data-dir>/debug/response-create-dumps` MUST be bounded on the base install path with no operator configuration. When the service captures an oversized `response.create` payload, it MUST NOT write a new dump if a dump for the same payload fingerprint is already stored, and after storing a dump it MUST remove the oldest stored dumps so that at most a fixed number of dump pairs remain. Each dump is a pair of a gzipped payload file and a meta file that MUST be added and removed together. Suppressing a duplicate MUST remain operator-visible in the logs, because the recurrence signal is the reason the dump path exists. The service MUST write a dump without blocking request handling: the rejection is returned to the client while the dump completes asynchronously, and the dump is logged as saved only once both files are written. Dumps MUST be written one at a time in the order they were requested, so duplicate suppression applies to a retry that arrives while the earlier dump is still being written. Pending dumps (the one being written included) MUST be bounded both by count and by the memory their payloads hold, because ingress admits request bodies far larger than the upstream limit. A dump that would exceed either bound, including a single payload larger than the memory budget, MUST be dropped and logged as a warning with its request id and payload size, so an operator knows that request has no dump; deciding and logging the drop MUST NOT encode or hash the payload on the request path. The meta file of a dump whose payload exceeds 1 MiB MUST NOT carry the payload field summary, because computing it parses the whole payload and stalls request handling; this includes every dump of a payload rejected for exceeding the upstream size limit. Such a meta file MUST still record the payload size and fingerprint, and the field summary is derived offline from the gzipped payload file. Graceful shutdown MUST wait for dumps that are being written or waiting, within what remains of its drain deadline or its post-drain cleanup reserve, whichever is later. A dump whose write has started MUST be allowed to complete. A dump still waiting when that time is up is not written: shutdown MUST log a warning with how many queued dumps were not written, and each such dump MUST be logged with its request id.

#### Scenario: Repeated identical payloads are stored once

- **GIVEN** an oversized `response.create` payload has already been dumped
- **WHEN** a retry of the byte-identical payload is dumped again
- **THEN** no additional dump pair is written
- **AND** the originally stored dump pair is retained
- **AND** the suppressed duplicate is logged with its payload fingerprint and the path of the existing dump

#### Scenario: Distinct payloads are stored separately

- **GIVEN** an oversized `response.create` payload has already been dumped or is waiting to be dumped
- **WHEN** a different oversized payload is dumped
- **THEN** a separate dump pair is written for it

#### Scenario: Dump does not block the rejection

- **GIVEN** an oversized `response.create` payload is rejected
- **WHEN** its dump is written
- **THEN** the rejection reaches the client without waiting for the dump to be hashed, compressed, or written

#### Scenario: Retry arriving during its dump is suppressed as a duplicate

- **GIVEN** the dump of an oversized payload is still being written
- **WHEN** a byte-identical retry is rejected
- **THEN** its dump waits for the first one and is then suppressed as a duplicate
- **AND** the suppression is logged with the payload fingerprint and the path of the existing dump

#### Scenario: Dump beyond the pending bounds is dropped

- **GIVEN** the maximum number of dumps, or payloads holding the maximum memory, are being written or waiting
- **WHEN** another oversized payload is rejected
- **THEN** the rejection is returned as usual and no dump is written for it
- **AND** a warning names that request and its payload size

#### Scenario: Payload larger than the memory budget is not retained

- **GIVEN** no dump is pending
- **WHEN** a payload larger than the pending-dump memory budget is rejected
- **THEN** no dump is queued for it and its text is not kept alive after the rejection
- **AND** a warning names that request and its payload size

#### Scenario: Shutdown lets a dump in progress finish

- **GIVEN** a dump is being written when graceful shutdown begins
- **WHEN** shutdown drains the service
- **THEN** it waits for that dump and any waiting dumps within the remaining drain deadline or post-drain reserve before it finishes
- **AND** if that time runs out, the dump being written still completes, and each waiting dump that is not written is logged with its request id together with a warning giving their count

#### Scenario: Oldest dumps are pruned once the directory is full

- **GIVEN** the dump directory already holds the maximum number of dump pairs
- **WHEN** a dump for a new payload is written
- **THEN** the oldest dump pairs are removed so the maximum is not exceeded
- **AND** each removed payload file has its meta file removed with it
- **AND** the newly written dump pair is retained

#### Scenario: Dump retention needs no setting

- **GIVEN** a default installation with no dump-related configuration
- **WHEN** oversized response-create dumps are captured over time
- **THEN** duplicate suppression and pruning apply
- **AND** no `CODEX_LB_*` setting is required to bound the directory
