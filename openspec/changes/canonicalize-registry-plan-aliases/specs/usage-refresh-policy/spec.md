## ADDED Requirements

### Requirement: Model-plan eligibility canonicalizes catalog plan aliases

Codex-lb MUST canonicalize known upstream plan aliases in the model catalog's allowed plans before comparing them with an account plan, so a catalog snapshot that still names `self_serve_business_prolite` admits `prolite` accounts.

#### Scenario: Legacy catalog snapshot names the raw alias

- **GIVEN** an account with stored `plan_type` `prolite`
- **AND** a persisted model-registry snapshot whose allowed plans for the model list `self_serve_business_prolite`
- **WHEN** codex-lb selects an account for that model
- **THEN** the account remains eligible

#### Scenario: Alias does not widen other plans

- **GIVEN** an account with stored `plan_type` `plus`
- **WHEN** the catalog allows only `self_serve_business_prolite`
- **THEN** the account is not eligible
