from __future__ import annotations

from dataclasses import dataclass

from app.core.clients.proxy import ProxyResponseError, is_confirmed_pre_dispatch_transport_error
from app.core.upstream_proxy import UpstreamProxyRouteError
from app.db.models import Account
from app.modules.proxy._service.http_bridge.protocol import _HTTPBridgeServiceProtocol
from app.modules.proxy._service.support import (
    _DeferredAccountBackoffLifecycle,
    is_account_gone,
    is_selected_account_removed_response,
    route_failure_response,
)
from app.modules.proxy.load_balancer import AccountLease


@dataclass
class _HTTPBridgePreDispatchFailover:
    """Bridge-session startup failover for confirmed dead account proxy routes.

    Only a transport failure that proves the upstream request never dispatched
    may move a session to another account. The failed account's stream lease
    is released before the bounded transient backoff floor is recorded; keyed
    requests defer that write until their singular reservation settles. A
    hard-required account fails closed on the original sanitized failure. The
    preserved ``last_error`` keeps that failure authoritative when selection
    cannot produce a replacement, instead of a generated ``no_accounts``.
    """

    excluded_account_ids: set[str]
    preferred_account_id: str | None
    reallocate_sticky: bool
    last_error: ProxyResponseError | None = None

    async def handle(
        self,
        service: _HTTPBridgeServiceProtocol,
        account: Account,
        lease: AccountLease | None,
        exc: ProxyResponseError,
        *,
        required_account: bool,
        deferred_account_backoff_lifecycle: _DeferredAccountBackoffLifecycle | None = None,
        defer_account_health_write: bool = False,
    ) -> bool:
        if is_selected_account_removed_response(exc):
            # The account was deleted after selection: nothing was sent and
            # the account is not at fault, so move on without a backoff.
            await service._load_balancer.release_account_lease(lease)
            if required_account:
                raise exc
            self._exclude(account, exc)
            return True
        if not is_confirmed_pre_dispatch_transport_error(exc):
            return False
        await service._load_balancer.release_account_lease(lease)
        if (
            defer_account_health_write
            and deferred_account_backoff_lifecycle is not None
            and not deferred_account_backoff_lifecycle.settlement_confirmed
        ):
            deferred_account_backoff_lifecycle.pending_backoffs.setdefault(account.id, account)
        else:
            await service._load_balancer.record_error_backoff(account)
        if required_account:
            raise exc
        self._exclude(account, exc)
        return True

    async def release_and_exclude(
        self,
        service: _HTTPBridgeServiceProtocol,
        account: Account,
        lease: AccountLease | None,
    ) -> None:
        """Release ``account``'s startup lease and reselect without it."""
        self.excluded_account_ids.add(account.id)
        self.preferred_account_id = None
        await service._load_balancer.release_account_lease(lease)

    async def settle_refresh_failure(
        self,
        service: _HTTPBridgeServiceProtocol,
        account: Account,
        lease: AccountLease | None,
        exc: BaseException,
        required_account: bool,
    ) -> bool:
        """Release the startup lease once after a failed forced refresh or re-open.

        No session owns ``lease`` yet. An account deleted meanwhile takes the
        selected-account-removed path (``True``: reselect without it; a required
        owner raises the removal error). Any other failure releases the lease
        and returns ``False`` for the caller to re-raise.
        """
        if isinstance(exc, UpstreamProxyRouteError) and is_account_gone(exc):
            return await self.handle(
                service, account, lease, route_failure_response(exc), required_account=required_account
            )
        await service._load_balancer.release_account_lease(lease)
        return False

    def _exclude(self, account: Account, exc: ProxyResponseError) -> None:
        self.last_error = exc
        self.excluded_account_ids.add(account.id)
        self.preferred_account_id = None
        self.reallocate_sticky = True
