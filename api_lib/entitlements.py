"""Entitlements read/write helpers (service role)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from supabase_rest import rest_get, rest_patch_filter, rest_post


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
