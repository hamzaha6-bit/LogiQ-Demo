"""Entitlements read/write helpers (service role)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from supabase_rest import rest_get, rest_patch_filter, rest_post

logger = logging.getLogger("logiq.entitlements")


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


def upsert_entitlement(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    row = dict(payload)
    row["updated_at"] = datetime.now(timezone.utc).isoformat()
    return rest_post("entitlements", row, on_conflict="client_id")


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


def apply_topup(client_id: str, actions_to_add: int) -> None:
    cid = (client_id or "").strip()
    add = int(actions_to_add)
    if not cid or add <= 0:
        logger.warning("Top-up skipped: invalid client_id or actions_to_add")
        return

    entitlement = get_entitlement(cid)
    if not entitlement or (entitlement.get("status") or "").strip().lower() != "active":
        logger.warning("Top-up skipped: client %s not active", cid)
        return

    current_limit = int(entitlement.get("actions_limit") or 0)
    new_limit = current_limit + add
    rest_patch_filter(
        "entitlements",
        {"client_id": f"eq.{cid}"},
        {
            "actions_limit": new_limit,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    logger.info(
        "Top-up applied: +%s actions for client %s, new limit: %s",
        add,
        cid,
        new_limit,
    )
