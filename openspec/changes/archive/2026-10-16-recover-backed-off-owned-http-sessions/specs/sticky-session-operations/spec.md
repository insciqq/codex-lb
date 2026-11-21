## ADDED Requirements

### Requirement: Transient backoff preserves required owner availability

When a request is bound to one resolved account by hard continuation ownership,
the service MUST keep that account eligible for the existing transient-error
backoff fallback. Hard continuation ownership means a resolved hard sticky
owner or a required continuity owner (such as a previous-response owner, or
an input-file owner when an existing HTTP bridge session reconnects). An
input-file pin on first HTTP bridge session creation is not carried as
continuity evidence; it keeps the upstream behaviour, so an owner in
transient backoff fails closed there. The fallback MUST NOT authorize an
account switch or clear health penalties merely to select the owner.
Persisted unavailability, account scope, security policy, concurrency caps,
and explicit caller exclusions MUST remain enforced. A soft preferred account, the account configured for single-account
routing, and a reuse probe that disables fallback only to test whether the
current account can serve the next turn are not ownership: they MUST keep
their error backoff, so the caller can select a healthy sibling instead.

#### Scenario: HTTP full resend recovers on its backed-off owner

- **GIVEN** an HTTP conversation whose complete history only its owner can serve
- **AND** its ACTIVE owner is within bounded transient-error backoff while another account is healthy
- **WHEN** the client resends that complete history on either Responses HTTP route
- **THEN** the request can select its original owner and complete
- **AND** the supplied history is forwarded without an anchor and without trimming
- **AND** opaque reasoning and completed tool-call/output pairs remain unchanged on that owner
- **AND** the returned turn state supports a later successful continuation

#### Scenario: A separate session can use a healthy sibling

- **WHEN** an unbound request arrives while another account serves transient backoff
- **THEN** the selector can serve it on a healthy sibling without changing an existing conversation's hard owner

#### Scenario: Soft preference, single-account routing, and reuse probes keep error backoff

- **GIVEN** account A is within transient-error backoff and account B is healthy
- **WHEN** A is only a soft preferred account, the account configured for single-account routing, or the current account of an open WebSocket or HTTP bridge session that a reuse probe checks
- **THEN** selection does not admit A through the backoff fallback
- **AND** a soft preference is served on B, single-account routing does not route to A until its backoff ends, and a reuse probe misses so its caller moves the turn or selects B

#### Scenario: Real unavailability still fails closed

- **WHEN** a required owner is quota-exceeded, paused, deactivated, or otherwise outside its existing eligibility constraints
- **THEN** transient backoff fallback does not admit that owner or silently move its account-bound history to a sibling

#### Scenario: Selection alone retains health evidence

- **WHEN** a required owner is selected through transient backoff fallback
- **THEN** its error counter and last-error timestamp remain intact until an existing recovery or outcome rule changes them
