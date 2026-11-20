## MODIFIED Requirements

### Requirement: Internal drain control authorizes the raw socket peer

The unauthenticated `/internal/drain/start`, `/internal/drain/stop`, and `/internal/drain/status` endpoints MUST authorize the caller against the launcher-preserved raw socket peer. A forwarded-client projection MUST NOT satisfy that authorization, regardless of which peers `FORWARDED_ALLOW_IPS` trusts. A request whose raw peer is not a loopback address MUST be rejected with 403, and MUST NOT begin drain, commit the shutdown barrier, or report drain state. If raw-peer capture is unavailable the request MUST be rejected on the same terms. A loopback raw peer MUST be rejected on the same terms when the request carries forwarding evidence (`Forwarded`, `X-Forwarded-*`, `X-Real-IP`, `X-Client-IP`, `X-Cluster-Client-IP`, `True-Client-IP`, `CF-Connecting-IP`, `Fastly-Client-IP`, `X-Envoy-External-Address` or `Via`), because a same-host reverse proxy connects over loopback on behalf of another client. A relay that adds no forwarding header is indistinguishable from a direct local caller, so same-host public proxies MUST NOT expose `/internal/drain/*`.

#### Scenario: Spoofed loopback from a remote peer is rejected

- **WHEN** forwarding projection trusts every peer
- **AND** a request from a remote transport peer supplies a loopback forwarded client address
- **AND** it posts to the drain-start endpoint with a drain-deadline header
- **THEN** the response is 403
- **AND** the process is not draining
- **AND** the one-way shutdown barrier is not committed

#### Scenario: Spoofed loopback cannot read or stop drain

- **WHEN** forwarding projection trusts every peer
- **AND** a request from a remote transport peer supplies a loopback forwarded client address
- **THEN** the drain-stop endpoint responds 403
- **AND** the drain-status endpoint responds 403

#### Scenario: Direct loopback caller is authorized

- **WHEN** a loopback caller without forwarding headers requests drain status
- **THEN** the request is authorized
- **AND** the response reports current drain state

#### Scenario: Same-host reverse proxy cannot relay drain control

- **WHEN** a request arrives from a loopback transport peer
- **AND** it carries a forwarded client address or another forwarding header
- **THEN** the drain-start, drain-stop and drain-status endpoints respond 403
- **AND** the process is not draining

#### Scenario: Missing raw-peer capture fails closed

- **WHEN** an internal drain endpoint receives a request without preserved raw socket provenance
- **THEN** the response is 403
