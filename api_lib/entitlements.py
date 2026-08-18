"""Entitlements read/write helpers (service role)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from supabase_rest import rest_get, rest_patch_filter, rest_post
from tiers import limits_for

logger = logging.getLogger("logiq.entitlements")


class EntitlementError(Exception):
    """Apply failed — callers must fail closed (HTTP 500), not ACK."""


def get_entitlement(client_id: str) -> Optional[Dict[str, Any]]:
    cid = (client_id or "").strip()
    if not cid:
        return None
    rows = rest_get("entitlements", {"client_id": f"eq.{cid}", "select": "*"})
    return rows[0] if rows else None


def get_entitlement_by_subscription_id(subscription_id: str) -> Optional[Dict[str, Any]]:
    sid = (subscription_id or "").strip()
    if not sid:
        return None
    rows = rest_get(
        "entitlements",
        {"stripe_subscription_id": f"eq.{sid}", "select": "*"},
    )
    return rows[0] if rows else None


def upsert_entitlement(payload: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(payload)
    row["updated_at"] = datetime.now(timezone.utc).isoformat()
    saved = rest_post("entitlements", row, on_conflict="client_id")
    if not saved:
        raise EntitlementError(
            f"Failed to upsert entitlement for client {row.get('client_id')}"
        )
    return saved


def member_user_ids(client_id: str) -> List[str]:
    rows = rest_get(
        "client_members",
        {"client_id": f"eq.{client_id}", "select": "user_id"},
    )
    return [str(row["user_id"]) for row in rows if row.get("user_id")]


def sync_user_profiles_plan(client_id: str, plan: Optional[str]) -> None:
    user_ids = member_user_ids(client_id)
    if not user_ids:
        return
    rest_patch_filter(
        "user_profiles",
        {"id": f"in.({','.join(user_ids)})"},
        {"plan": plan},
    )


def purchased_topup_actions(existing: Optional[Dict[str, Any]]) -> int:
    """Pack extras that must survive subscription.updated plan-limit rebuilds."""
    if not existing:
        return 0
    stored = existing.get("purchased_topup_actions")
    try:
        stored_int = int(stored) if stored is not None else 0
    except (TypeError, ValueError):
        stored_int = 0
    if stored_int > 0:
        return stored_int
    plan_limit = int(limits_for(existing.get("plan"))["actions"])
    current = 0
    try:
        current = int(existing.get("actions_limit") or 0)
    except (TypeError, ValueError):
        current = 0
    return max(0, current - plan_limit)


def apply_topup(client_id: str, actions_to_add: int) -> Dict[str, int]:
    cid = (client_id or "").strip()
    try:
        add = int(actions_to_add)
    except (TypeError, ValueError) as exc:
        raise EntitlementError("invalid topup_actions") from exc
    if not cid or add <= 0:
        raise EntitlementError("invalid client_id or actions_to_add")

    entitlement = get_entitlement(cid)
    if not entitlement or (entitlement.get("status") or "").strip().lower() != "active":
        raise EntitlementError(f"Top-up requires an active entitlement for client {cid}")

    current_limit = int(entitlement.get("actions_limit") or 0)
    new_limit = current_limit + add
    new_bonus = purchased_topup_actions(entitlement) + add
    ok = rest_patch_filter(
        "entitlements",
        {"client_id": f"eq.{cid}"},
        {
            "actions_limit": new_limit,
            "purchased_topup_actions": new_bonus,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    if not ok:
        raise EntitlementError(f"Failed to apply top-up for client {cid}")
    logger.info(
        "Top-up applied: +%s actions for client %s, new limit: %s",
        add,
        cid,
        new_limit,
    )
    return {"actions_limit": new_limit, "purchased_topup_actions": new_bonus}
