"""Which local seat, and which egress route, a Codex caller's token uses.

``/api/codex/usage`` and the Codex reset-credit consume forward the caller's
own ChatGPT bearer token upstream. Seats of a team workspace share the
``chatgpt-account-id`` header, so the header alone does not say whose token
it is, and each seat may be bound to a different proxy pool.

The caller is identified by the identity claims in its token. They are read
without verifying the signature, which is safe for this purpose: they only
choose the route of the caller's own request, upstream validates the token
itself, and altering them breaks the token, so no valid token can be steered
onto another seat's route. Claims of the wrong type are rejected rather than
ignored, so a malformed token can never widen what it may reach.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import TokenEncryptor
from app.core.exceptions import ProxyAuthError, ProxyUpstreamError
from app.core.upstream_proxy import (
    ACCOUNT_NOT_FOUND_REASON,
    ResolvedUpstreamRoute,
    UpstreamProxyRouteError,
    resolve_upstream_route,
)
from app.db.models import Account, AccountStatus
from app.db.session import get_background_session
from app.modules.accounts.repository import AccountsRepository

_AUTH_CLAIM = "https://api.openai.com/auth"
# Every claim that names the user behind a token. ``chatgpt_account_user_id``
# names the user within one workspace; ``sub`` is the login principal.
_IDENTITY_CLAIMS = ("chatgpt_user_id", "user_id", "chatgpt_account_user_id")
_INACTIVE_STATUSES = frozenset({AccountStatus.PAUSED, AccountStatus.DEACTIVATED})

UNKNOWN_SEAT_MESSAGE = "Unknown or inactive chatgpt-account-id"
_ROUTE_MESSAGE = "Unable to resolve upstream proxy route for ChatGPT credentials"


class MalformedClaimsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TokenClaims:
    account_id: str | None
    identities: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Candidate:
    account_id: str
    installation_id: str
    allow_pending_deletion: bool


@dataclass(frozen=True, slots=True)
class CodexUsageSeat:
    """The seat chosen for a caller and the route agreed before dispatch."""

    account_id: str
    email: str
    route: ResolvedUpstreamRoute | None
    # True when the token identified exactly this seat; otherwise the token
    # may belong to any candidate and every candidate resolved to ``route``.
    identified: bool
    candidates: tuple[_Candidate, ...]

    @property
    def candidate_ids(self) -> frozenset[str]:
        return frozenset(candidate.account_id for candidate in self.candidates)


def token_claims(token: str) -> TokenClaims:
    """Account and identity claims of an unverified JWT; empty for opaque tokens.

    Raises ``MalformedClaimsError`` when a claim this module reads has the
    wrong type.
    """
    payload = _jwt_payload(token)
    if payload is None:
        return TokenClaims(account_id=None, identities=frozenset())
    auth = payload.get(_AUTH_CLAIM)
    if auth is None:
        auth = {}
    if not isinstance(auth, dict):
        raise MalformedClaimsError(_AUTH_CLAIM)
    account_ids = {
        value for value in (_claim(auth, "chatgpt_account_id"), _claim(payload, "chatgpt_account_id")) if value
    }
    if len(account_ids) > 1:
        raise MalformedClaimsError("chatgpt_account_id")
    identities = {_claim(auth, name) for name in _IDENTITY_CLAIMS}
    identities |= {_claim(payload, name) for name in (*_IDENTITY_CLAIMS, "sub")}
    return TokenClaims(
        account_id=next(iter(account_ids), None),
        identities=frozenset(value for value in identities if value),
    )


def _jwt_payload(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        decoded = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _claim(claims: dict[str, Any], name: str) -> str | None:
    value = claims.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MalformedClaimsError(name)
    return value.strip() or None


def stored_seat_claims(account: Account, encryptor: TokenEncryptor) -> TokenClaims:
    """What a local seat is known by: its stored identity column plus the
    account and identity claims of its stored ID token (the only record for
    rows imported before those columns were filled)."""
    try:
        claims = token_claims(encryptor.decrypt(account.id_token_encrypted))
    except Exception:
        claims = TokenClaims(account_id=None, identities=frozenset())
    if account.chatgpt_user_id:
        claims = TokenClaims(claims.account_id, claims.identities | {account.chatgpt_user_id})
    return claims


def seat_is_active(account: Account) -> bool:
    return account.status not in _INACTIVE_STATUSES and account.delete_requested_at is None


async def select_codex_usage_seat(session: AsyncSession, header_account_id: str, token: str) -> CodexUsageSeat:
    """Choose the seat and route for ``token`` before any upstream request.

    * A token naming another ChatGPT account than the header is rejected.
    * A token identifying exactly one active seat uses that seat's route.
    * A token identifying only inactive seats is rejected.
    * Any other token may belong to any candidate (the identified copies, or
      every seat of the account when none matched); it is sent only when all
      of them, inactive bound seats included, resolve to the same egress.
    """
    try:
        claims = token_claims(token)
    except MalformedClaimsError as exc:
        raise ProxyAuthError("Invalid ChatGPT token or chatgpt-account-id") from exc
    if claims.account_id is not None and claims.account_id != header_account_id:
        raise ProxyAuthError("Invalid ChatGPT token or chatgpt-account-id")
    encryptor = TokenEncryptor()
    seats: list[Account] = []
    matched: list[Account] = []
    for row in await AccountsRepository(session).list_codex_usage_seats(header_account_id):
        stored = stored_seat_claims(row, encryptor)
        is_match = bool(claims.identities & stored.identities)
        # Rows without a ChatGPT account id predate that column; they belong
        # to this caller only when the identity says so, and never when their
        # ID token names another ChatGPT account (the user and login claims
        # are the same in every workspace a person belongs to).
        if row.chatgpt_account_id is None and (not is_match or stored.account_id not in (None, header_account_id)):
            continue
        seats.append(row)
        if is_match:
            matched.append(row)
    if matched:
        active_matched = [seat for seat in matched if seat_is_active(seat)]
        if not active_matched:
            raise ProxyAuthError(UNKNOWN_SEAT_MESSAGE)
        if len(active_matched) == 1:
            selected = active_matched[0]
            candidate = _Candidate(selected.id, selected.codex_installation_id, allow_pending_deletion=False)
            route = await _agreed_route(session, (candidate,))
            return CodexUsageSeat(selected.id, selected.email, route, identified=True, candidates=(candidate,))
        pool = matched
    else:
        pool = seats
    active = [seat for seat in pool if seat_is_active(seat)]
    if not active:
        raise ProxyAuthError(UNKNOWN_SEAT_MESSAGE)
    candidates = tuple(_Candidate(seat.id, seat.codex_installation_id, allow_pending_deletion=True) for seat in pool)
    route = await _agreed_route(session, candidates)
    selected = active[0]
    return CodexUsageSeat(selected.id, selected.email, route, identified=False, candidates=candidates)


async def revalidate_codex_usage_route(seat: CodexUsageSeat) -> ResolvedUpstreamRoute | None:
    """Re-check the route right before another dispatch of the caller's token.

    Bindings may change while a request runs. The caller's token is only
    forwarded again when its candidates still agree on the egress it was
    validated through; any change fails closed.
    """
    async with get_background_session() as session:
        route = await _agreed_route(session, seat.candidates)
    if _egress(route) != _egress(seat.route):
        raise ProxyUpstreamError(_ROUTE_MESSAGE)
    return route


async def _agreed_route(session: AsyncSession, candidates: Iterable[_Candidate]) -> ResolvedUpstreamRoute | None:
    routes = [await _candidate_route(session, candidate) for candidate in candidates]
    if len({_egress(route) for route in routes}) != 1:
        raise ProxyUpstreamError(_ROUTE_MESSAGE)
    return routes[0]


async def _candidate_route(session: AsyncSession, candidate: _Candidate) -> ResolvedUpstreamRoute | None:
    try:
        return await resolve_upstream_route(
            session,
            account_id=candidate.account_id,
            operation="usage_identity",
            scope="account",
            encryptor=TokenEncryptor(),
            # The seats were listed by an earlier statement; a deletion or a
            # re-import since then must not turn a bound seat into direct egress.
            require_account=True,
            installation_id=candidate.installation_id,
            allow_pending_deletion=candidate.allow_pending_deletion,
        )
    except UpstreamProxyRouteError as exc:
        if exc.reason == ACCOUNT_NOT_FOUND_REASON and not candidate.allow_pending_deletion:
            # The identified seat itself is gone.
            raise ProxyAuthError(UNKNOWN_SEAT_MESSAGE) from exc
        raise ProxyUpstreamError(_ROUTE_MESSAGE) from exc


def _egress(route: ResolvedUpstreamRoute | None) -> str | None:
    return route.pool_id if route is not None else None
