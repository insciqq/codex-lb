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
- The value is classified before the Bearer and Basic token passes run. It
  is exempt only when it is a whole Bearer or Basic token (followed by
  whitespace, `,`, `&` or the end of the line, optionally after a closing
  quote) or already the output of the token passes (`Bearer [REDACTED]`,
  `Basic [REDACTED]`, `[REDACTED]`, the JSON pass's `"[REDACTED]"`). A value
  that merely begins with `Bearer` (`Bearer-x a, ...`, `Bearer, ...`,
  `Bearer "..."`) or whose token runs into other characters
  (`Bearer abc%2B...`, `Bearer [REDACTED]GLUED`) fails closed.
- Accepting the passes' own output is what keeps redaction idempotent. The
  error-response logger redacts the message with `_redact_log_value` and the
  WARNING+ formatter redacts the rendered line again; the second pass must
  not drop `, status=failed` or the closing quote. A literal
  `Bearer [REDACTED], response=...` input therefore renders exactly as a real
  `Bearer abc, response=...` does: the same-line tail after a whole Bearer
  token stays, as before this change (class 3 of the issue).
- A double quote, or an escaped `\"` from a header map serialized into a JSON
  string field, may sit between the key and the separator. Single-quoted
  Python-repr keys stay with the Python-repr pattern, which now keeps only a
  value replaced whole (`'[REDACTED]'`, `'Basic [REDACTED]'`); a value masked
  in part, such as a Digest `uri="/p?token=[REDACTED]", response="..."`, is
  replaced whole.
- The JSON pass's `"[REDACTED]"` is also accepted escaped (`\"[REDACTED]\"`):
  the error-response logger quotes a redacted JSON message before the
  formatter's second pass, and fields after a nested header map must survive.
- An empty value (`Authorization: , x`) is left unchanged, and key/value
  whitespace is matched possessively so a long space run stays linear.
- Known limits: a header pair without a separator (`Authorization Digest ...`),
  a fully percent-encoded header (`Authorization%3A%20Digest...`), and a Basic
  credential whose unencoded password contains a comma are not recognised.
  Cookie and Set-Cookie values in header dumps are not masked: a regex over
  arbitrary header dumps (list-valued Set-Cookie, encoded line breaks,
  `sid = value`) is open-ended and outside this change.

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
