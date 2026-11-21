# Tasks

## 1. Exhausted-retry stream terminals

- [x] 1.1 Render the post-refresh transient terminal, the account-model
      rejection retry terminal and the outer retries-exhausted terminal
      through `_response_failed_event_from_upstream_error`, keeping
      `resets_at` while preserving the existing code, message, and response id.

## 2. Spec + tests

- [x] 2.1 MODIFIED `responses-api-compat` requirement: exhausted-retry stream
      terminals keep the upstream reset timestamp.
- [x] 2.2 Focused unit coverage for the helper and the post-refresh terminal.
- [x] 2.3 Integration coverage: an HTTP stream whose every account fails with
      a retryable rate-limit error keeps `resets_at` on the retries-exhausted
      terminal, and the last-attempt pass-through keeps it too.
