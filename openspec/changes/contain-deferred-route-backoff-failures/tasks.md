## 1. Implementation

- [x] 1.1 Contain ordinary failures of post-settlement deferred route-backoff writes.

## 2. Verification

- [x] 2.1 Stream regression: one terminal when the backoff write fails once or twice.
- [x] 2.2 Cancellation regression: the cancellation propagates instead of the backoff error.
