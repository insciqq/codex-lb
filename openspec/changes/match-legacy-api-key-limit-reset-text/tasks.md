## 1. Implementation

- [x] 1.1 Match `reset_at` against the lossless spellings of the expected value on SQLite.
- [x] 1.2 Page the sweep by `(reset_at, id)` on the deadline index and cover today's `T` range.
- [x] 1.3 Cover legacy spellings, sub-millisecond differences, a concurrent change, the same-day `T` case and the query plan.
