from __future__ import annotations

from typing import Any


def runtime_basic_auth_url(user: str, value: str, authority: str) -> str:
    """Build a credential-bearing proxy URL at runtime for redaction tests."""
    return "http://" + user + ":" + value + "@" + authority


def use_rowless_request_path_routes(monkeypatch: Any) -> None:
    """Resolve request-path routes as for an account without a database row.

    These suites drive the proxy with in-memory accounts that have no row in
    the database the route lookup reads. The request path's lookup requires
    the selected row (``require_account``), which such an account can never
    satisfy, so the lookup keeps its binding-only behavior here. Deleted and
    re-imported accounts on the request path are covered with real rows in
    ``tests/integration/test_deleted_account_failover.py``.
    """
    from app.modules.proxy import service as proxy_service

    resolve = proxy_service.resolve_upstream_route

    async def rowless(session: Any, **kwargs: Any) -> Any:
        kwargs.pop("require_account", None)
        kwargs.pop("installation_id", None)
        return await resolve(session, **kwargs)

    monkeypatch.setattr(proxy_service, "resolve_upstream_route", rowless)


class WindowsOSError(OSError):
    """OSError carrying a typed ``winerror``, as CPython raises on Windows."""

    winerror: int

    def __init__(self, winerror: int, message: str = "Windows socket failure") -> None:
        super().__init__(message)
        self.winerror = winerror
