from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.modules.proxy._load_balancer.sticky_selection import SelectionInputsProtocol

CONTINUITY_OWNER_UNAVAILABLE = "continuity_owner_unavailable"
CONTINUITY_OWNER_POLICY_CONFLICT = "continuity_owner_policy_conflict"


def required_continuity_owner_failure(
    selection_inputs: SelectionInputsProtocol,
    *,
    required_account_id: str,
) -> tuple[str, str] | None:
    if selection_inputs.error_code is not None:
        return None
    eligible_ids = {account.id for account in selection_inputs.effective_continuity_owner_candidates} | {
        account.id for account in selection_inputs.accounts
    }
    if required_account_id in eligible_ids:
        return None
    runtime_accounts = (
        selection_inputs.accounts if selection_inputs.runtime_accounts is None else selection_inputs.runtime_accounts
    )
    if required_account_id not in {account.id for account in runtime_accounts}:
        return CONTINUITY_OWNER_UNAVAILABLE, "Required continuity owner account no longer exists"
    return CONTINUITY_OWNER_POLICY_CONFLICT, "Required continuity owner is outside the eligible account policy"
