from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable

from app.core.metrics import prometheus as metrics
from app.db.models import AccountStatus

logger = logging.getLogger(__name__)


def publish_account_status_counts(statuses: Iterable[AccountStatus]) -> None:
    """Export ``codex_lb_accounts_total{status}`` from one full inventory read.

    ``statuses`` must cover every account that is not pending deletion (the
    accounts the dashboard lists). Every status is written, zeros included,
    so a status whose last account moved elsewhere stops reporting its old
    count. No-op when the Prometheus client is absent. Never raises: callers
    publish from the middle of routing and refresh work that must not fail
    because of a metric.
    """
    gauge = metrics.accounts_total
    if gauge is None:
        return
    try:
        counts = Counter(statuses)
        for status in AccountStatus:
            gauge.labels(status=status.value).set(counts.get(status, 0))
    except Exception:
        logger.warning("account status metric update failed", exc_info=True)
