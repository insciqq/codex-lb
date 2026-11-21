"""Strict upstream proxy route resolution for Codex/OpenAI egress."""

from app.core.upstream_proxy.resolver import (
    ACCOUNT_NOT_FOUND_REASON,
    UpstreamProxyRouteError,
    resolve_proxy_endpoint,
    resolve_upstream_route,
)
from app.core.upstream_proxy.types import ResolvedProxyEndpoint, ResolvedUpstreamRoute, sends_plaintext_credentials

__all__ = [
    "ACCOUNT_NOT_FOUND_REASON",
    "ResolvedProxyEndpoint",
    "ResolvedUpstreamRoute",
    "sends_plaintext_credentials",
    "UpstreamProxyRouteError",
    "resolve_proxy_endpoint",
    "resolve_upstream_route",
]
