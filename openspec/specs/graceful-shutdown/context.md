# Graceful shutdown context

## Bounded managed HTTP client shutdown

`close_http_client()` force-closes the current and retired managed clients,
then waits for their completion events. Each managed client owns its transport
cleanup. Cancelling the waiter does not repair a stalled TLS or TCP close.
The application therefore waits at most 10 seconds, matching the nearby
leader-lease wait, and proceeds while retaining the task until it settles.
With a committed shutdown deadline, the HTTP close, the metrics-server stop and
the `close_db()` teardown-registry drain are all bounded against one cutoff:
the shared deadline minus the response.create dump margin. For example, with
18 seconds left the HTTP close gets at most 2 seconds, the metrics stop 5 and
the registry drain 10, so the dump settlement still starts with its 1-second
margin before force-exit. `close_db_teardown_wait_seconds()` bounds only the
registry drain; `engine.dispose()` is not bounded.
A done callback releases the reference and consumes any failure.

For example, when one managed client's completion event stays unset, bridge
membership has already been marked stale but the old lifespan remains stuck
before process-death marking and database disposal. The bounded wait allows
those later steps to execute. It does not promise to forcibly terminate the
transport or to bound other independent shutdown steps.

Related work is complementary: #2506 grants database-owning scheduler tasks a
shutdown grace period; #1443 drains audit/fleet tasks; #2030 handles SQLite
teardown completion. None bounds this managed HTTP close wait.

Verification covers prompt close, timeout without cancellation, self-cancellation,
late failure consumption, and actual application lifespan teardown after a
stalled close. The lifespan regression fails on the current unmodified base.
