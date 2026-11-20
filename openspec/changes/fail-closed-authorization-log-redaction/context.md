# Fail-closed authorization log redaction -- change context

## Purpose / scope

Stop auth-param lists (Digest, AWS SigV4) from leaking their later members in
rendered log text. Resolves the auth-param-list and quoted-key classes of
#2028. Class 3 (a whitespace tail after a Bearer token) is out of scope.

## Decisions

- Policy chosen by the maintainer: fail closed. For an authorization value
  whose scheme is not Bearer or Basic, redact to the end of the current line.
  Patterns already run per CR/LF line, so a following line is never consumed.
- A grammar-aware parser of auth-param lists was rejected. The earlier
  attempt in #2009 grew to about 900 lines without converging.
- Basic keeps the `,`/`&` bound from the existing pattern. That behavior is
  pinned by existing tests (`Authorization: Basic dXNlcjpwYXNz, status=failed`
  keeps `, status=failed`), and Basic has no auth-param list to protect.
- The value is classified as it arrived, before the Bearer and Basic token
  passes run. It is exempt only when a real Bearer or Basic token follows the
  scheme, or when the JSON pass already replaced the whole quoted value. A
  value that merely begins with `Bearer` (such as `Bearer-x a, response=...`,
  `Bearer, ...` or `Bearer "..."`) and one that arrives already reading
  `Bearer [REDACTED], ...` or `Basic [REDACTED], ...` fail closed.
- Known limit: redaction is not idempotent for text that already holds the
  comma-bounded output of the token passes. A second pass over
  `Authorization: [REDACTED], status=failed` drops `, status=failed`, because
  the pattern cannot tell its own output from a value that arrived reading
  `[REDACTED]`. This is reached when an error message redacted by
  `_redact_log_value` is logged at WARNING+ and the formatter redacts the line
  again. The second pass only removes same-line diagnostic text; it never
  reveals a credential.
- Only a double quote may sit between the key and the separator. Single-quoted
  Python-repr keys stay with the Python-repr pattern.

## Trade-off

Same-line diagnostic text after a non-Basic, non-Bearer authorization value is
dropped, for example `authorization='Digest ...', status=failed` renders as
`authorization=[REDACTED]`.

## Example

Before:

```text
Authorization: Digest username="svc", realm="api", nonce="n1", response="6629fae4"
-> Authorization: [REDACTED], realm="api", nonce="n1", response="6629fae4"
{"authorization": Digest username="public", malformed, response="QA_SECRET"}
-> unchanged
```

After:

```text
Authorization: [REDACTED]
{"authorization": [REDACTED]
```

Unchanged: `Authorization: Basic dXNlcjpwYXNz, status=failed` renders as
`Authorization: [REDACTED], status=failed`.

## Related

- Fixes #2028 (classes 1 and 2).
- Builds on the line-scoped redaction change.
