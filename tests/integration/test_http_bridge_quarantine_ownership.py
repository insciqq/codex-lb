"""A completing bridge request clears only quarantine evidence it observed for itself.

Issue 2268: quarantine generations restarted at zero for every re-created
entry, so a completion that captured its fence before the key's entry was
removed could match a replacement entry armed for another session and clear
it. The fence also did not tell which session owned the evidence, and an
inactive entry was popped regardless of the fence, erasing an eventless
strike recorded while the completion was still awaiting settlement.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Literal

import pytest

from app.dependencies import get_proxy_service_for_app
from app.modules.proxy._service.http_bridge import quarantine
from tests.integration.test_http_responses_bridge import (
    _cleanup_http_bridge_sessions,  # noqa: F401
    _collect_sse_events,
    _promotion_history,
)
from tests.integration.test_http_responses_bridge import (
    promotion_transport as promotion_transport,
)

pytestmark = pytest.mark.integration

_WEDGED = quarantine._HTTP_BRIDGE_QUARANTINE_WEDGED_REATTACH_REASON
_Stage = Literal["preload", "registration"]


def _install_completion_hook(monkeypatch, service, stage: _Stage, action) -> list[bool]:
    """Run ``action(session)`` once inside the first response's completion.

    ``preload`` runs it in the completion's pre-settle circuit load, before the
    quarantine fence is captured; ``registration`` runs it in the anchor
    registration await, after the fence is captured.
    """
    fired: list[bool] = []
    processing_completion = False
    process = service._process_http_bridge_upstream_text

    async def process_and_track(session, text, *args, **kwargs):
        nonlocal processing_completion
        processing_completion = json.loads(text).get("type") == "response.completed"
        try:
            return await process(session, text, *args, **kwargs)
        finally:
            processing_completion = False

    hook_name = (
        "_load_http_bridge_retry_circuit" if stage == "preload" else "_register_http_bridge_previous_response_id"
    )
    original = getattr(service, hook_name)

    async def hooked(session, *args, **kwargs):
        result = await original(session, *args, **kwargs)
        if processing_completion and not fired:
            fired.append(True)
            action(session)
        return result

    monkeypatch.setattr(service, "_process_http_bridge_upstream_text", process_and_track)
    monkeypatch.setattr(service, hook_name, hooked)
    return fired


async def _complete_first_turn(async_client) -> None:
    body = {"model": "gpt-5.4", "instructions": "test", "stream": True, "input": _promotion_history()}
    events = await _collect_sse_events(async_client, "/v1/responses", json_body=body)
    assert events[-1]["type"] == "response.completed"


@pytest.mark.asyncio
async def test_old_completion_keeps_a_quarantine_armed_for_the_replacement_session(
    async_client, app_instance, promotion_transport, monkeypatch
) -> None:
    """The completion observes a quarantine armed for the session that replaced its own."""
    service = get_proxy_service_for_app(app_instance)
    replacements = []

    def replace_and_quarantine(session) -> None:
        replacement = dataclasses.replace(session)
        replacements.append((session, replacement))
        service._http_bridge_sessions[session.key] = replacement
        quarantine._quarantine_http_bridge_session(service, replacement, reason=_WEDGED)

    fired = _install_completion_hook(monkeypatch, service, "preload", replace_and_quarantine)
    await _complete_first_turn(async_client)

    assert fired
    original, replacement = replacements[0]
    try:
        assert quarantine._http_bridge_session_key_quarantined(service, original.key)
        assert replacement.quarantined is True
    finally:
        service._http_bridge_sessions[original.key] = original


@pytest.mark.asyncio
async def test_completion_clears_the_quarantine_its_own_session_observed(
    async_client, app_instance, promotion_transport, monkeypatch
) -> None:
    """Control: evidence the completion observed for its own session is disproved."""
    service = get_proxy_service_for_app(app_instance)

    def quarantine_own_session(session) -> None:
        quarantine._quarantine_http_bridge_session(service, session, reason=_WEDGED)

    fired = _install_completion_hook(monkeypatch, service, "preload", quarantine_own_session)
    await _complete_first_turn(async_client)

    assert fired
    session = next(iter(service._http_bridge_sessions.values()))
    assert not quarantine._http_bridge_session_key_quarantined(service, session.key)
    assert session.key not in quarantine._http_bridge_quarantine_registry(service)


@pytest.mark.asyncio
async def test_completion_fence_does_not_match_a_recreated_entry(
    async_client, app_instance, promotion_transport, monkeypatch
) -> None:
    """The observed entry is removed and the key re-quarantined before the clear.

    The re-created entry must carry a generation the captured fence never saw,
    even though it is the entry's first arm.
    """
    service = get_proxy_service_for_app(app_instance)
    original_sessions = []

    def quarantine_before_capture(session) -> None:
        original_sessions.append(session)
        quarantine._quarantine_http_bridge_session(service, session, reason=_WEDGED)

    def recreate_after_capture(session) -> None:
        registry = quarantine._http_bridge_quarantine_registry(service)
        registry.pop(session.key)
        quarantine._quarantine_http_bridge_session(service, dataclasses.replace(session), reason=_WEDGED)

    preload_fired = _install_completion_hook(monkeypatch, service, "preload", quarantine_before_capture)
    registration_fired = _install_completion_hook(monkeypatch, service, "registration", recreate_after_capture)
    await _complete_first_turn(async_client)

    assert preload_fired and registration_fired
    assert quarantine._http_bridge_session_key_quarantined(service, original_sessions[0].key)


@pytest.mark.asyncio
async def test_completion_keeps_an_eventless_strike_recorded_after_its_fence(
    async_client, app_instance, promotion_transport, monkeypatch
) -> None:
    service = get_proxy_service_for_app(app_instance)

    def strike(session) -> None:
        quarantine._record_http_bridge_quarantine_eventless_timeout(service, session)

    fired = _install_completion_hook(monkeypatch, service, "registration", strike)
    await _complete_first_turn(async_client)

    assert fired
    session = next(iter(service._http_bridge_sessions.values()))
    entry = quarantine._http_bridge_quarantine_registry(service).get(session.key)
    assert entry is not None, "the completion erased a strike recorded after its fence"
    assert entry.consecutive_eventless_timeouts == 1
    assert not quarantine._http_bridge_session_key_quarantined(service, session.key)

    # The surviving strike is what makes the next eventless timeout consecutive.
    quarantine._record_http_bridge_quarantine_eventless_timeout(service, session)
    assert quarantine._http_bridge_session_key_quarantined(service, session.key)


@pytest.mark.asyncio
async def test_completion_resets_an_eventless_strike_recorded_before_its_fence(
    async_client, app_instance, promotion_transport, monkeypatch
) -> None:
    """Control: a strike the completion observed is reset by the healthy response."""
    service = get_proxy_service_for_app(app_instance)

    def strike(session) -> None:
        quarantine._record_http_bridge_quarantine_eventless_timeout(service, session)

    fired = _install_completion_hook(monkeypatch, service, "preload", strike)
    await _complete_first_turn(async_client)

    assert fired
    session = next(iter(service._http_bridge_sessions.values()))
    assert session.key not in quarantine._http_bridge_quarantine_registry(service)
