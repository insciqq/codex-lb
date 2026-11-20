## ADDED Requirements

### Requirement: Usage refresh stamps the attempt that supplied the payload

A persisted usage-refresh sample MUST carry the time captured immediately before dispatching the fetch attempt that returned its payload. A retry after a credential refresh MUST NOT reuse the failed attempt's time. Live usage samples MUST keep the time they were enqueued.

#### Scenario: Live sample recorded during a credential refresh
- **GIVEN** a usage fetch that fails with 401
- **AND** a live 100% sample recorded while the credential refresh runs
- **WHEN** the retried fetch returns 0% usage
- **THEN** the retried sample is the latest usage row for the account
- **AND** the account balancer can select the account
