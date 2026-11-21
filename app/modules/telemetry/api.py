from __future__ import annotations

import asyncio
import logging
import platform
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.core.auth.dependencies import (
    require_dashboard_write_access,
    set_dashboard_error_format,
    validate_dashboard_session,
)
from app.db.session import get_session
from app.modules.telemetry.consent import ResolvedConsent, TelemetryConsentStore
from app.modules.telemetry.schemas import (
    ActiveConsentState,
    TelemetryConsentResponse,
    TelemetryConsentUpdate,
    TelemetrySnapshotEnvelope,
    build_snapshot_envelope,
)
from app.modules.telemetry.sender import TelemetrySender
from app.modules.telemetry.snapshot import TelemetrySnapshotBuilder, deployment_method

logger = logging.getLogger(__name__)

_OPT_OUT_TASKS: set[asyncio.Task[None]] = set()

router = APIRouter(
    prefix="/api/settings",
    tags=["dashboard"],
    dependencies=[Depends(validate_dashboard_session), Depends(set_dashboard_error_format)],
)


@router.get("/telemetry", response_model=TelemetryConsentResponse)
async def get_telemetry_consent(
    include_preview: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
) -> TelemetryConsentResponse:
    store = TelemetryConsentStore(session)
    consent = await store.resolve()
    return await _response(
        session,
        store,
        consent,
        include_preview=include_preview or (consent.state == "undecided" and consent.source == "default"),
    )


@router.put("/telemetry", response_model=TelemetryConsentResponse)
async def update_telemetry_consent(
    payload: TelemetryConsentUpdate = Body(...),
    _write_access=Depends(require_dashboard_write_access),
    session: AsyncSession = Depends(get_session),
) -> TelemetryConsentResponse:
    store = TelemetryConsentStore(session)
    previous = await store.resolve()
    consent = await store.set_decision(payload.enabled)
    if previous.active and not consent.active:
        try:
            identity = await store.get_or_create_identity()
            task = asyncio.create_task(
                TelemetrySender().send_opt_out(
                    identity,
                    app_version=__version__,
                    deployment_mode=deployment_method(),
                    os_arch=f"{platform.system().lower()}/{platform.machine().lower()}",
                ),
                name="anonymous-telemetry-opt-out",
            )
            _OPT_OUT_TASKS.add(task)
            task.add_done_callback(_handle_opt_out_task_done)
        except Exception as exc:
            logger.debug("Unable to schedule anonymous telemetry opt-out", exc_info=exc)
    return await _response(session, store, consent, include_preview=False)


async def _response(
    session: AsyncSession,
    store: TelemetryConsentStore,
    consent: ResolvedConsent,
    *,
    include_preview: bool,
) -> TelemetryConsentResponse:
    preview: TelemetrySnapshotEnvelope | None = None
    if include_preview:
        snapshot_consent = _preview_consent(consent)
        snapshot = await TelemetrySnapshotBuilder(session).build(
            await _preview_instance_id(store, consent),
            consent=snapshot_consent,
        )
        preview = build_snapshot_envelope(snapshot)
    return TelemetryConsentResponse(
        state=consent.state,
        source=consent.source,
        active=consent.active,
        preview=preview,
    )


async def _preview_instance_id(store: TelemetryConsentStore, consent: ResolvedConsent) -> str:
    """Instance id shown in a preview.

    Active telemetry uses, and if needed mints, the persisted identity it sends
    with. While telemetry is inactive a preview must not create the identity or
    its signing key before an opt-in: it shows the persisted id when one exists
    and otherwise a throwaway random UUID.
    """
    if consent.active:
        return (await store.get_or_create_identity()).instance_id
    return await store.existing_instance_id() or str(uuid4())


def _preview_consent(consent: ResolvedConsent) -> ActiveConsentState:
    """Consent value the next transmitted snapshot would carry once telemetry is active.

    Only the unset default (``undecided``, ``source: default``) transmits
    ``undecided``. Every opt-in from an inactive state transmits ``enabled``:
    a dashboard enable persists ``enabled``, and ``CODEX_LB_TELEMETRY_ENABLED=true``
    resolves to ``enabled``. The fork default (``false``) is never lifted back
    to the undecided-means-active default.
    """
    return "undecided" if consent.state == "undecided" else "enabled"


def _handle_opt_out_task_done(task: asyncio.Task[None]) -> None:
    try:
        if task.cancelled():
            return
        if exc := task.exception():
            logger.debug(
                "Anonymous telemetry opt-out background task failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
    finally:
        _OPT_OUT_TASKS.discard(task)
