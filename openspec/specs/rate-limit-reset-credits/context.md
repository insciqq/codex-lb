# Rate-limit reset credits context

The normative contract is in [spec.md](spec.md).

## Snapshots are per replica

Reset-credit snapshots live in process memory and each replica's refresh loop
rebuilds them. Right after a restart, or after that loop failed for an
account, the serving replica has no snapshot for it. A missing snapshot is not
evidence of an empty credit bank, so the consume endpoint lets its fresh
pre-consume fetch decide; only a present snapshot that shows no available
credit refuses without calling upstream. The overview keeps reporting
`availableResetCredits: 0` for an account the replica has not refreshed yet.

Example: a replica restarts, the dashboard shows one banked credit from the
live usage payload, and the operator presses Reset. The handler fetches the
credit list, pins the soonest available credit to the request's
`redeem_request_id`, and consumes it.

## Known limit: callers without a redeem request id

Idempotency rests on the durable `(account_id, redeem_request_id) -> credit_id`
pin. A caller that sends no `redeem_request_id` gets a fresh synthesized id on
every attempt, so a retry of a request whose response was lost is a new
redemption. Before the missing-snapshot fall-through, such a retry landing on a
replica without a snapshot was refused; now it performs the fresh fetch and may
redeem the next available credit. Clients that retry should send a stable
`redeemRequestId`; the dashboard does.
