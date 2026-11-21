## ADDED Requirements

### Requirement: A terminal append left past its bound keeps its own attempt identity

A terminal transcript append that outlived its live-delivery bound MUST NOT
write under the operation context of a newer terminal attempt for the same
operation. Once a newer attempt owns the operation, the older append MUST
return a not-persisted, settlement-required result without appending its
terminal event. A failed write of a queued-event batch MUST drop the
operation's transcript and queued events only while the context that owned the
batch still owns the operation.

#### Scenario: Newer attempt registers while the older append drains

- **GIVEN** a terminal append is still draining queued events after its bound expired
- **AND** a newer terminal attempt for the same operation registered its own context
- **WHEN** the older append finishes draining
- **THEN** it does not append its terminal event
- **AND** only the newer attempt's terminal event is written, under the newer attempt's owner epoch

#### Scenario: A newer attempt keeps its transcript when an old batch write fails

- **GIVEN** an expired attempt's queued-event write is still running
- **AND** a newer attempt for the same operation queued events and started its terminal append
- **WHEN** the old write fails
- **THEN** the newer attempt's queued events and terminal append are persisted
