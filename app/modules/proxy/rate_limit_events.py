from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from app.core.config.settings_cache import get_settings_cache
from app.core.types import JsonValue
from app.core.usage.live_snapshots import LiveUsageWindow, parse_rate_limit_headers
from app.core.utils.sse import extract_sse_event_name
from app.modules.api_keys.service import ApiKeyData

CODEX_RATE_LIMITS_EVENT = "codex.rate_limits"


@dataclass(frozen=True, slots=True)
class CodexQuotaView:
    """How the origin instance shows native quota events to one client.

    ``hidden`` omits every quota event. Otherwise a default-family event is
    rebuilt from the pooled ``headers``; an event the pool cannot represent
    (another limit family, or no pooled primary/secondary window) is passed
    through unchanged, because the client may see quota and the pool has
    nothing to replace it with.
    """

    hidden: bool
    headers: Mapping[str, str]


async def hide_upstream_quota_for_api_key_clients(api_key: ApiKeyData | None) -> bool:
    if api_key is None:
        return False
    settings = await get_settings_cache().get()
    return settings.hide_upstream_quota_from_api_keys


async def rate_limit_headers_for_client(
    api_key: ApiKeyData | None,
    load_headers: Callable[[], Awaitable[dict[str, str]]],
) -> dict[str, str]:
    if await hide_upstream_quota_for_api_key_clients(api_key):
        return {}
    return await load_headers()


async def codex_quota_view_for_client(
    api_key: ApiKeyData | None,
    load_headers: Callable[[], Awaitable[dict[str, str]]],
) -> CodexQuotaView:
    if await hide_upstream_quota_for_api_key_clients(api_key):
        return CodexQuotaView(hidden=True, headers={})
    return CodexQuotaView(hidden=False, headers=await load_headers())


def is_codex_rate_limits_block(payload: Mapping[str, JsonValue], event_block: str) -> bool:
    """Recognise a quota event by its JSON type or, when the JSON has none, by its SSE event name."""
    event_type = payload.get("type")
    if isinstance(event_type, str):
        return event_type == CODEX_RATE_LIMITS_EVENT
    return CODEX_RATE_LIMITS_EVENT in event_block and extract_sse_event_name(event_block) == CODEX_RATE_LIMITS_EVENT


def project_codex_rate_limit_event(
    payload: Mapping[str, JsonValue],
    headers: Mapping[str, str],
) -> dict[str, JsonValue] | None:
    """Replace account telemetry with the downstream pool snapshot, never merge it."""
    for discriminator in ("limit_id", "metered_limit_name", "limit_name"):
        if payload.get(discriminator) not in (None, "codex"):
            return None
    snapshot = parse_rate_limit_headers(headers)
    if snapshot is None or not snapshot.has_windows:
        return None
    event: dict[str, JsonValue] = {
        "type": "codex.rate_limits",
        "rate_limits": {
            "primary": _event_window(snapshot.primary),
            "secondary": _event_window(snapshot.secondary),
        },
    }
    if snapshot.credits_has is not None and snapshot.credits_unlimited is not None:
        event["credits"] = {
            "has_credits": snapshot.credits_has,
            "unlimited": snapshot.credits_unlimited,
            "balance": headers.get("x-codex-credits-balance"),
        }
    return event


def _event_window(window: LiveUsageWindow | None) -> dict[str, JsonValue] | None:
    if window is None:
        return None
    return {
        "used_percent": window.used_percent,
        "window_minutes": window.window_minutes,
        "reset_at": window.reset_at,
    }
