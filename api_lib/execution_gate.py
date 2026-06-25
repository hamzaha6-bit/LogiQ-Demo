"""Execution gate — entitlement and usage checks before agent actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from entitlements import get_entitlement
from supabase_rest import client_id_from_user_id
from usage import get_monthly_usage, record_action as _record_client_action

# Provisional per-action cost estimates (pence). Tune when real usage data is available.
ACTION_COST_PENCE: Dict[str, int] = {
    "blueprint_chat": 20,
    "agent_action": 10,
    "integration": 2,
    "action": 10,
}


@dataclass
class GateResult:
    allowed: bool
    reason: str = ""
    client_id: str = ""
    error: str = ""

    def as_error_payload(self) -> Dict[str, str]:
        return {"error": self.error, "message": self.reason}


def action_cost_pence(action_type: str) -> int:
    return ACTION_COST_PENCE.get(action_type, ACTION_COST_PENCE["action"])


def check_execution_gate(user_id: str, action_type: str = "action") -> GateResult:
    uid = (user_id or "").strip()
    if not uid:
        return GateResult(
            allowed=False,
            reason="Authentication required",
            error="unauthenticated",
        )

    try:
        client_id = client_id_from_user_id(uid)
    except ValueError as exc:
        return GateResult(
            allowed=False,
            reason=str(exc),
            error="no_client_membership",
            client_id="",
        )

    entitlement = get_entitlement(client_id)
    if not entitlement:
        return GateResult(
            allowed=False,
            reason="Please subscribe to continue using Vision.",
            error="no_active_subscription",
            client_id=client_id,
        )

    status = (entitlement.get("status") or "").strip().lower()
    if status != "active":
        return GateResult(
            allowed=False,
            reason="Please subscribe to continue using Vision.",
            error="no_active_subscription",
            client_id=client_id,
        )

    actions_limit = int(entitlement.get("actions_limit") or 0)
    spend_cap_pence = int(entitlement.get("spend_cap_pence") or 0)
    usage = get_monthly_usage(client_id)
    cost_pence = action_cost_pence(action_type)

    if actions_limit > 0 and usage["actions_used"] >= actions_limit:
        return GateResult(
            allowed=False,
            reason=f"You've used all {actions_limit} actions this month. Top up to continue.",
            error="action_limit_reached",
            client_id=client_id,
        )

    projected_spend = usage["spend_pence"] + cost_pence
    if spend_cap_pence > 0 and projected_spend > spend_cap_pence:
        return GateResult(
            allowed=False,
            reason="Monthly spend cap reached. Top up to continue.",
            error="spend_cap_reached",
            client_id=client_id,
        )

    return GateResult(allowed=True, client_id=client_id)


def record_allowed_action(client_id: str, action_type: str) -> None:
    _record_client_action(client_id, action_cost_pence(action_type))
