## MODIFIED Requirements

### Requirement: Secret-pattern redaction stays on the current line

Keyed, bearer, basic, authorization, JSON, and Python-repr secret patterns
MUST be applied independently to each CR/LF-delimited line of rendered log
text. A match MUST NOT consume CR or LF or any text from a following line.
Unterminated JSON secret values MUST be redacted through the end of the
current line. A Bearer credential MUST treat a glued `:` tail on the same
line as credential material. Same-line comma and ampersand separators MUST
keep their existing truncation behavior for keyed values and for
authorization values that are a whole Bearer or Basic token, or already the
token patterns' own output (`Bearer [REDACTED]`, `Basic [REDACTED]` or
`[REDACTED]`). A token is whole only when whitespace, `,`, `&` or the end of
the line follows it, optionally after a closing quote. Every other
authorization value, including one whose scheme is not Bearer or Basic, one
that only begins with `Bearer` or `Basic` without a whole token, and one whose
key is followed by a closing double quote, escaped or not, before the
separator, MUST be redacted through the end of the current line. An empty
authorization value MUST be left unchanged. Applying redaction again to its
own output, as the error-response logger and the formatter do, MUST NOT change
it. Records below WARNING MUST still skip these keyed patterns.

#### Scenario: Authorization does not swallow the next traceback line

- **GIVEN** a WARNING or higher record whose exception text contains
  `authorization=Basic X` followed by a newline and `status=failed`
- **WHEN** the text or JSON formatter renders the record
- **THEN** the Basic credential is replaced with `[REDACTED]`
- **AND** the following line still contains `status=failed`

#### Scenario: Unterminated JSON secret is redacted through end of line

- **GIVEN** a WARNING or higher record contains `{"token":"abc` with no
  closing quote before the line ending, then a following `safe diagnostic line`
- **WHEN** the text or JSON formatter renders the record
- **THEN** the token value is replaced with `[REDACTED]`
- **AND** `safe diagnostic line` remains

#### Scenario: Bearer glued colon tail is redacted

- **GIVEN** a WARNING or higher record contains
  `Bearer abc.def:GLUEDTAIL, status=502`
- **WHEN** the text or JSON formatter renders the record
- **THEN** the rendered text contains `Bearer [REDACTED], status=502`
- **AND** neither `abc.def` nor `GLUEDTAIL` appears

#### Scenario: Same-line authorization truncation is unchanged

- **GIVEN** a WARNING or higher record contains
  `Authorization: Basic dXNlcjpwYXNz status=failed` on one line with no comma
- **WHEN** the text formatter renders the record
- **THEN** the credential is redacted
- **AND** `status=failed` is not present on that line

#### Scenario: Line terminators are preserved and redaction is idempotent

- **GIVEN** rendered secret-bearing text that uses LF and CRLF separators
- **WHEN** secret-pattern redaction is applied once and then again
- **THEN** the terminator bytes and line count are unchanged
- **AND** the second pass equals the first

#### Scenario: Digest auth-param list is redacted through end of line

- **GIVEN** a WARNING or higher record contains
  `Authorization: Digest username="svc", realm="api", nonce="n1", response="6629fae4"`
- **WHEN** the text or JSON formatter renders the record
- **THEN** the rendered text contains `Authorization: [REDACTED]`
- **AND** neither `realm=` nor `response=` nor `6629fae4` appears

#### Scenario: AWS SigV4 auth-param list is redacted through end of line

- **GIVEN** a WARNING or higher record contains
  `Authorization: AWS4-HMAC-SHA256 Credential=AKID/aws4_request, SignedHeaders=host;x-amz-date, Signature=fe5f80f7`
- **WHEN** the text or JSON formatter renders the record
- **THEN** the rendered text contains `Authorization: [REDACTED]`
- **AND** neither `Signature=` nor `fe5f80f7` appears

#### Scenario: Quoted authorization key with a Digest value is redacted

- **GIVEN** a WARNING or higher record contains
  `{"authorization": Digest username="public", malformed, response="QA_SECRET"}`
- **WHEN** the text or JSON formatter renders the record
- **THEN** the rendered text contains `{"authorization": [REDACTED]`
- **AND** `QA_SECRET` does not appear

#### Scenario: Basic credential keeps the comma bound

- **GIVEN** a WARNING or higher record contains
  `Authorization: Basic dXNlcjpwYXNz, status=failed`
- **WHEN** the text formatter renders the record
- **THEN** the rendered text contains `Authorization: [REDACTED], status=failed`

#### Scenario: Malformed Bearer value fails closed

- **GIVEN** a WARNING or higher record contains
  `Authorization: Bearer-x a, response=SECRET`
- **WHEN** the text formatter renders the record
- **THEN** the rendered text contains `Authorization: [REDACTED]`
- **AND** neither `response=` nor `SECRET` appears

#### Scenario: A token without a terminator fails closed

- **GIVEN** a WARNING or higher record contains
  `Authorization: Bearer abc%2BSECRET, status=failed` or
  `Authorization: Bearer [REDACTED]SECRET, status=failed`
- **WHEN** the text formatter renders the record
- **THEN** the rendered text contains `Authorization: [REDACTED]`
- **AND** `SECRET` does not appear

#### Scenario: Escaped authorization key inside a JSON string field

- **GIVEN** a WARNING or higher record contains a header map serialized into a
  JSON string field, `{\"authorization\": \"Digest username=u, response=SECRET\"}`
- **WHEN** the text or JSON formatter renders the record
- **THEN** `SECRET` does not appear

#### Scenario: Error-response logging redacts once

- **GIVEN** an error message containing
  `Authorization: Basic dXNlcjpwYXNz, status=failed`
- **WHEN** the error-response logger redacts the message and the WARNING
  formatter renders the record
- **THEN** the rendered message reads `Authorization: [REDACTED], status=failed`
  with its closing quote
- **AND** a token patterns' output such as `Authorization: Bearer [REDACTED]`
  is unchanged by the second pass
- **AND** for a JSON message `{"headers": {"Authorization": "Bearer x"}, "retry_after": 10}`
  the quoted message keeps `retry_after` after both passes

### Requirement: Rendered log records redact URL userinfo and keyed secrets

Every log record rendered by the application's text, access, and JSON
formatters MUST have URL userinfo, in both the `scheme://user:password@` and
the username-only `scheme://user@` forms, replaced with
`scheme://[REDACTED]@` and `Basic <token>` authorization tokens (in the
`Basic`, `basic` and `BASIC` scheme spellings)
(a reversible encoding of `user:password`, as carried in aiohttp proxy-error
reprs) replaced with `Basic [REDACTED]`, regardless of the originating logger
(application, `asyncio`, aiohttp, uvicorn) and including exception and stack
text. Structured extra keys MUST be redacted like values. Records at WARNING
level or higher MUST additionally have keyed secrets (`password=`, `token=`,
`api_key=`, bearer, basic and authorization values in any letter case, JSON
secret fields embedded in strings, and structured extra fields whose key names
a secret, whatever the value type) redacted. Redaction MUST never raise and
MUST fail closed: if a redaction pass fails, the affected text is replaced with
a `[REDACTED: log redaction failed]` placeholder and the record is still
emitted with its timestamp, level and logger; structured extras that are cyclic,
pathologically deep, or unprintable MUST still be emitted with redaction
applied to every finite, printable part. Application startup MUST route
`warnings.warn` output through the same log handlers. Log records that contain
no secret patterns MUST render byte-identically to the unredacted rendering.

#### Scenario: Unclosed aiohttp connection repr is credential-free

- **GIVEN** an aiohttp connection is finalized without release and its connection key holds a credentialed proxy URL
- **WHEN** the loop exception handler logs `Unclosed connection` through the `asyncio` logger
- **THEN** the rendered record contains `proxy=URL('scheme://[REDACTED]@host:port')`
- **AND** the password appears in neither the text nor the JSON rendering

#### Scenario: Userinfo containing unencoded sub-delims is redacted

- **GIVEN** a proxy password containing an RFC 3986 sub-delim such as `'`, which yarl leaves unencoded in the URL userinfo, or a raw environment proxy string that is not percent-encoded at all
- **WHEN** the URL is rendered in any log record at any level
- **THEN** the record contains `scheme://[REDACTED]@host:port` and neither the raw nor the percent-encoded password

#### Scenario: Username-only URL userinfo is redacted

- **GIVEN** a URL whose userinfo carries only a username (a token used as the username, `scheme://user@host:port`) with no `:password` part
- **WHEN** the URL is rendered in any log record at any level, text or JSON
- **THEN** the record contains `scheme://[REDACTED]@host:port` and the username does not appear

#### Scenario: Proxy error repr with a Basic token is masked

- **GIVEN** an aiohttp proxy error whose tunnel request headers carry `Proxy-Authorization: Basic <token>`
- **WHEN** the error is logged with `%r` at any level, or its repr is logged by the loop's exception handler for an unretrieved task
- **THEN** the rendered record contains `'Proxy-Authorization': 'Basic [REDACTED]'`
- **AND** neither the token nor the password appears in the text or JSON rendering

#### Scenario: Secret-keyed structured extras are masked

- **GIVEN** a WARNING or higher record carries an extra field such as `{"password": "..."}` or `{"access_token": "..."}`
- **WHEN** the JSON formatter renders the record
- **THEN** the field value is replaced with `[REDACTED]` whatever its type (string, list, number, bytes, mapping); a null value stays null
- **AND** fields such as `attempt` or `tokens` keep their values
- **AND** an extra key carrying URL userinfo is rendered as `scheme://[REDACTED]@host`

#### Scenario: Secret-free records are unchanged

- **WHEN** a record such as the one-time bootstrap token banner contains no URL userinfo or keyed secret pattern
- **THEN** the rendered output is byte-identical to the unredacted rendering

#### Scenario: Redaction failure never breaks logging and fails closed

- **WHEN** a redaction pass raises while rendering a record
- **THEN** the record is still emitted, in text and JSON, with its timestamp, level and logger
- **AND** the affected text is rendered as `[REDACTED: log redaction failed]` rather than the original text

#### Scenario: Cyclic or unprintable structured extras never drop the record

- **GIVEN** a record carries an extra whose container refers back to itself, or whose `repr()` raises
- **WHEN** the JSON formatter renders the record
- **THEN** the record is emitted, the back-reference collapses to a `{...}` / `[...]` placeholder and the unprintable value to an `<unprintable ...>` marker
- **AND** secret-keyed fields and URL userinfo in the finite part of the extra are still redacted

#### Scenario: Secret-keyed mappings rendered as Python repr are masked

- **GIVEN** a WARNING or higher record renders a mapping with `%r`, or the JSON formatter falls back to text for a structured extra (nesting beyond the depth limit, a container whose iteration raises, an unserializable rebuild)
- **WHEN** the rendered text contains `'password': 'x'`, `'access_token': [...]` or `'api_key': 123`
- **THEN** each secret-keyed value is replaced with `[REDACTED]` (quotes kept for quoted strings)
- **AND** `'Proxy-Authorization': 'Basic <token>'` keeps rendering as `'Basic [REDACTED]'`
- **AND** a secret-keyed value that an earlier pass masked only in part, such as a Digest value whose `uri` carries `token=`, is replaced whole
