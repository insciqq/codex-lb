from __future__ import annotations

import base64
import json

import pytest

from app.core.auth.codex_usage_seats import MalformedClaimsError, token_claims

pytestmark = pytest.mark.unit


def _jwt(claims: object) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"e30.{body}.sig"


def test_token_claims_collects_every_identity_claim() -> None:
    claims = token_claims(
        _jwt(
            {
                "sub": "auth0|principal",
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "workspace",
                    "chatgpt_account_user_id": "user-1__workspace",
                    "chatgpt_user_id": "user-1",
                    "user_id": "user-1",
                },
            }
        )
    )

    assert claims.account_id == "workspace"
    assert claims.identities == {"auth0|principal", "user-1__workspace", "user-1"}


@pytest.mark.parametrize("token", ["opaque-token", "a.b", "e30.!!!.sig", _jwt(["not", "an", "object"])])
def test_token_claims_of_a_non_jwt_are_empty(token: str) -> None:
    claims = token_claims(token)

    assert claims.account_id is None
    assert claims.identities == frozenset()


@pytest.mark.parametrize(
    "claims",
    [
        {"https://api.openai.com/auth": {"chatgpt_account_id": "a"}, "chatgpt_account_id": "b"},
        {"https://api.openai.com/auth": {"user_id": 1}},
        {"https://api.openai.com/auth": ["user-1"]},
        {"chatgpt_account_user_id": {"id": "user-1"}},
    ],
    ids=["conflicting-accounts", "int-identity", "list-auth", "object-identity"],
)
def test_token_claims_reject_contradictory_or_wrongly_typed_claims(claims: dict) -> None:
    with pytest.raises(MalformedClaimsError):
        token_claims(_jwt(claims))
