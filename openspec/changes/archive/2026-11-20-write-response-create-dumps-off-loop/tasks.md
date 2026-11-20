## 1. Off-loop write

- [x] 1.1 Submit dumps to a worker thread with a snapshot of the recorded request fields
- [x] 1.2 Write dumps one at a time in request order through a queue bounded by count and retained bytes; log a dropped dump with its size
- [x] 1.3 Skip the whole-payload meta summary above a fixed size

## 2. Shutdown

- [x] 2.1 Settle dumps after the database close, within the remaining shared deadline minus a margin; stop and log queued dumps at the cutoff; report a running write

## 3. Tests

- [x] 3.1 Rejections return while dumps are held; distinct payloads are written one at a time
- [x] 3.2 A retry queued behind its dump is suppressed with its fingerprint and existing path
- [x] 3.3 A dump beyond the count or byte bound is dropped with its size, without hashing on the loop
- [x] 3.4 A large payload is not parsed for the summary
- [x] 3.5 Lifespan shutdown closes the database before waiting for dumps and completes a dump that fits the budget
- [x] 3.6 Under the graceful server, a running dump at the deadline is reported and does not force an exit
