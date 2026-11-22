"""Turn-scoped local pre-dispatch refusal verdicts of the HTTP bridge (issue 2388)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import nullcontext
from typing import Any, cast

import pytest

from app.core.clients.proxy import ProxyResponseError
from app.modules.proxy import service as proxy_service
from app.modules.proxy._service.http_bridge.helpers import (
    _HTTP_BRIDGE_PRE_SUBMIT_FAILURE_ATTR,
    _http_bridge_owner_lookup_unavailable_error,
)


@pytest.mark.asyncio
async def test_waiters_sharing_one_refusal_get_their_own_turn_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every waiter of one session creation receives the same refusal instance.

    One turn already sent a frame upstream and must keep the transport
    lifecycle; the other sent nothing and must receive the refusal as a local
    pre-dispatch refusal. Neither verdict may reach the other turn or change
    the shared instance.
    """
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    shared_creation: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    both_waiting = asyncio.Event()
    waiting: list[bool] = []
    refusal = _http_bridge_owner_lookup_unavailable_error()
    setattr(refusal, _HTTP_BRIDGE_PRE_SUBMIT_FAILURE_ATTR, True)

    async def wait_on_shared_creation(
        *_args: Any, _turn_dispatch: Any, frame_sent: bool, **_kwargs: Any
    ) -> AsyncIterator[str]:
        _turn_dispatch.generating_frame_sent = frame_sent
        waiting.append(frame_sent)
        if len(waiting) == 2:
            both_waiting.set()
        await shared_creation
        yield ""

    monkeypatch.setattr(service, "_stream_via_http_bridge_impl", wait_on_shared_creation)

    async def run_turn(frame_sent: bool) -> ProxyResponseError:
        with pytest.raises(ProxyResponseError) as exc_info:
            async for _ in service._stream_via_http_bridge(frame_sent=frame_sent):
                pass
        return exc_info.value

    turns = [asyncio.create_task(run_turn(frame_sent)) for frame_sent in (True, False)]
    await asyncio.wait_for(both_waiting.wait(), timeout=5)
    shared_creation.set_exception(refusal)
    sent_turn, unsent_turn = await asyncio.gather(*turns)

    assert [sent_turn.local_pre_dispatch_refusal, unsent_turn.local_pre_dispatch_refusal] == [False, True]
    assert sent_turn is refusal
    assert unsent_turn is not refusal
    assert refusal.local_pre_dispatch_refusal is False
    assert (unsent_turn.status_code, unsent_turn.payload) == (refusal.status_code, refusal.payload)
    assert getattr(unsent_turn, _HTTP_BRIDGE_PRE_SUBMIT_FAILURE_ATTR, False) is True
