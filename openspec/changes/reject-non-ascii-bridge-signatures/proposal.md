## Why

The internal bridge route compared received signature headers with
`hmac.compare_digest` on `str` values. Header values arrive latin-1 decoded,
and `compare_digest` raises `TypeError` for a string with non-ASCII
characters, so a garbled or forged signature header produced a 500 before
API-key authentication instead of the invalid-signature response.

## What Changes

- A non-ASCII signature header value is treated as a non-matching signature;
  ASCII values keep the constant-time comparison.

## Impact

Internal bridge forwarding only. No setting, schema or API change.
