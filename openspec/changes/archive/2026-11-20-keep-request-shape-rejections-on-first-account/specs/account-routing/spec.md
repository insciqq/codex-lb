## MODIFIED Requirements

### Requirement: Upstream rejections of the request payload are account neutral

When upstream rejects a request because of the request payload itself, the proxy MUST NOT mutate the selected account's health: it MUST NOT record a transient account error, a rate-limit penalty, a quota penalty, or a permanent failure for that account. An upstream failure qualifies as a payload rejection only when it would reproduce identically on every account. The proxy MUST decide membership from the classified upstream message, never from the `invalid_request_error` code alone, and MUST require the upstream HTTP status to be 400 whenever a status is known. An upstream missing-tool-output rejection — the `invalid_request_error` whose message identifies a tool call with no matching tool output — MUST qualify. The proxy MUST also leave account health untouched for the model-entitlement rejection `The '<model>' model is not supported when using Codex with a ChatGPT account.`: that rejection is scoped to the named model and is not evidence about the account's ability to serve the models it is entitled to. Because upstream delivers that rejection on the streaming path with neither an error `code` nor an error `type`, which normalizes to the `upstream_error` fallback, the proxy MUST decide it from the message and the 400 status alone and MUST NOT require a particular normalized error code. An upstream request-shape rejection — an HTTP 400 whose entire message is `Stream must be set to true`, delivered with no error code (normalized to `upstream_error`) or as `invalid_request_error` — MUST qualify, and the proxy MUST classify it `non_retryable` so it is surfaced from the account that served it instead of being retried or failed over to other accounts; the same sentence with any other status or under any other code MUST keep its existing classification. Skipping the penalty MUST be logged so the decision is observable. Apart from the request-shape rejection, it MUST NOT change the failure classification or the failover decision, and it MUST NOT change the status and body returned to the client.

#### Scenario: Missing-tool-output rejection leaves account health untouched

- **GIVEN** account A is selected for a request whose input references a tool call with no matching tool output
- **WHEN** upstream returns HTTP 400 `invalid_request_error` with a missing-tool-output message
- **THEN** the proxy does not increment account A's transient error count and does not mark it rate-limited, quota-exceeded, or permanently failed
- **AND** the failure is still classified `non_retryable` and surfaced to the client unchanged

#### Scenario: Repeated client payload rejection cannot starve unrelated sessions

- **GIVEN** one client repeatedly re-sends the same payload that upstream rejects for a missing tool output
- **WHEN** those requests are served by accounts shared with other sessions
- **THEN** no serving account enters error backoff because of that payload
- **AND** a session hard-pinned to one of those accounts is not failed with a saturated-hard-affinity selection error caused by that payload

#### Scenario: Model-entitlement rejection leaves account health untouched

- **GIVEN** account A is selected for a model it is not entitled to use
- **WHEN** upstream returns HTTP 400 stating the model is not supported when using Codex with a ChatGPT account, with the error code normalized to `upstream_error` or to `invalid_request_error`
- **THEN** the proxy does not increment account A's transient error count and does not mark it rate-limited, quota-exceeded, or permanently failed
- **AND** the skip is logged

#### Scenario: Model-entitlement rejection still fails over

- **GIVEN** account A returned the model-entitlement rejection for the requested model
- **WHEN** the proxy classifies that failure
- **THEN** the classification and failover decision are unchanged, so an account with a different entitlement is still attempted
- **AND** the status and body returned to the client when every attempt is exhausted are unchanged

#### Scenario: A model no source can serve cannot poison subscription accounts

- **GIVEN** a model that resolves to no enabled model source and therefore reaches subscription account selection
- **WHEN** a client polls that model repeatedly and every subscription account returns the model-entitlement rejection
- **THEN** no serving account enters error backoff because of those rejections
- **AND** unrelated traffic hard-pinned to those accounts is not denied with a continuity-owner-unavailable or no-available-accounts selection error caused by them

#### Scenario: Model-entitlement rejection still penalizes the account

- **GIVEN** account A is selected for a request
- **WHEN** upstream fails with a non-400 status whose message matches the model-entitlement rejection
- **THEN** the proxy records the account-health penalty for account A as before, because only a genuine HTTP 400 qualifies as the model-scoped rejection

#### Scenario: A genuine upstream failure still penalizes the account

- **GIVEN** account A is selected for a request
- **WHEN** upstream fails with an `upstream_error` whose message is not the model-entitlement rejection
- **THEN** the proxy records the account-health penalty for account A as before

#### Scenario: Request-shape rejection surfaces without failover or penalty

- **GIVEN** several eligible accounts and a request whose shape upstream rejects
- **WHEN** the first account returns HTTP 400 with the message `Stream must be set to true` and no error code
- **THEN** the proxy classifies the failure `non_retryable` and surfaces it to the client without dispatching to another account
- **AND** no account's transient error count, error backoff, rate-limit, quota, or permanent-failure state changes

#### Scenario: Parameter rejection keeps failing over

- **GIVEN** account A rejects a request with a code-less HTTP 400 `Unsupported parameter: <name>` and account B accepts that parameter
- **WHEN** the proxy classifies A's failure
- **THEN** the classification and failover decision are unchanged, so B is attempted and serves the request

#### Scenario: Request-shape sentence on a server failure keeps its class

- **GIVEN** account A is selected for a request
- **WHEN** upstream fails with HTTP 500 and the message `Stream must be set to true`
- **THEN** the failure keeps its `retryable_transient` classification and the account-health penalty is recorded as before
