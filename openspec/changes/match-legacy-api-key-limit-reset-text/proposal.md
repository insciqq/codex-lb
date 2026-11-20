## Why

On SQLite, API-key limit rows written by older binders store `reset_at` in
other text spellings of the same naive UTC value (a `T` separator, trailing
fractional zeros trimmed, no seconds). They parse to the same datetime as the
bound `...SS.ffffff` form but never equal it as text, so the guarded updates
for the expired-limit sweep, usage reservation, reservation adjustment and
manual reset missed those rows: the sweep no longer spins on them (upstream PR
2654), but their limits never reset and reservations against them failed.

The sweep also paged by limit id, so with nothing due it read every limit,
and a `T`-spelled deadline earlier on the current day sorted after `now` and
was reset up to a day late.

## What Changes

- On SQLite, match the stored text against every lossless spelling of the
  expected deadline. Equal values always match; different values, including
  ones that differ below the millisecond, never do. Guarded updates stay
  primary-key lookups. PostgreSQL compares timestamps natively, unchanged.
- Page the sweep in `(reset_at, id)` order on `idx_api_key_limits_reset_at`,
  keep the stored sort key of the last selected row as the cursor, and on
  SQLite also sweep today's `T`-spelled range below `now`.
- The guarded write stores the canonical form, so legacy rows heal on their
  next reset. No schema or migration change.

## Impact

- Affected capability: `api-keys`.
- Affected code: `app/modules/api_keys/repository.py`.
