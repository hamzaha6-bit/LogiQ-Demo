"""Owner-only admin dashboard aggregates."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from supabase_rest import email_from_user_id, rest_get

# Mirror of frontend BILLING_PLANS amounts (pence). Keep in sync when pricing changes.
TIER_MRR_PENCE: Dict[str, int] = {
    "spark": 2000,
    "starter": 4900,
    "pro": 14900,
    "business": 39900,
}


def _owner_emails() -> set[str]:
    raw = (os.environ.get("OWNER_EMAILS") or "").strip()
    if not raw:
        return set()
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def email_is_owner(email: Optional[str]) -> bool:
    addr = (email or "").strip().lower()
    if not addr:
        return False
    owners = _owner_emails()
    return bool(owners) and addr in owners


def user_is_owner(user_id: str) -> bool:
    uid = (user_id or "").strip()
    if not uid:
        return False
    try:
        return email_is_owner(email_from_user_id(uid))
    except Exception:
        return False


def _month_start_iso() -> str:
    today = datetime.now(timezone.utc).date()
    return today.replace(day=1).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except Exception:
        return None


def build_admin_dashboard(user_id: str) -> Tuple[int, Dict[str, Any]]:
    uid = (user_id or "").strip()
    if not uid:
        return 401, {"detail": "Authentication required"}
    if not user_is_owner(uid):
        return 403, {"detail": "Owner access required"}

    entitlements = rest_get(
        "entitlements",
        {"status": "eq.active", "select": "*"},
    ) or []

    month = _month_start_iso()
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    total_actions = 0
    active_this_month = 0
    mrr_pence = 0
    clients_out: List[Dict[str, Any]] = []

    for ent in entitlements:
        client_id = str(ent.get("client_id") or "").strip()
        if not client_id:
            continue

        plan = (ent.get("plan") or "").strip().lower()
        mrr_pence += int(TIER_MRR_PENCE.get(plan, 0))

        usage_rows = rest_get(
            "client_usage",
            {
                "client_id": f"eq.{client_id}",
                "month": f"eq.{month}",
                "select": "actions_used,spend_pence,updated_at",
            },
        ) or []
        actions_used = int(usage_rows[0].get("actions_used") or 0) if usage_rows else 0
        usage_updated = _parse_iso(usage_rows[0].get("updated_at")) if usage_rows else None
        total_actions += actions_used
        if actions_used > 0 or (usage_updated and usage_updated >= cutoff):
            active_this_month += 1

        members = rest_get(
            "client_members",
            {
                "client_id": f"eq.{client_id}",
                "select": "user_id,role,created_at",
                "order": "created_at.asc",
            },
        ) or []
        primary = members[0] if members else {}
        primary_uid = str(primary.get("user_id") or "")
        email = email_from_user_id(primary_uid) if primary_uid else ""

        vertical = None
        joined_at = primary.get("created_at")
        if primary_uid:
            profiles = rest_get(
                "user_profiles",
                {
                    "id": f"eq.{primary_uid}",
                    "select": "onboarding_vertical,created_at",
                },
            ) or []
            if profiles:
                vertical = profiles[0].get("onboarding_vertical")
                if not joined_at:
                    joined_at = profiles[0].get("created_at")

        client_rows = rest_get(
            "clients",
            {"id": f"eq.{client_id}", "select": "created_at,name"},
        ) or []
        if client_rows and client_rows[0].get("created_at"):
            joined_at = client_rows[0].get("created_at")

        # Fallback last_active: client_usage.updated_at → entitlements.updated_at → joined
        last_active = None
        if usage_updated:
            last_active = usage_rows[0].get("updated_at")
        if not last_active:
            last_active = ent.get("updated_at") or ent.get("current_period_start") or joined_at

        # Prefer audit_log if any rows exist for member users
        for m in members:
            mid = str(m.get("user_id") or "")
            if not mid:
                continue
            audit = rest_get(
                "audit_log",
                {
                    "user_id": f"eq.{mid}",
                    "select": "created_at",
                    "order": "created_at.desc",
                    "limit": "1",
                },
            ) or []
            if audit and audit[0].get("created_at"):
                last_active = audit[0].get("created_at")
                break

        member_ids = [str(m.get("user_id")) for m in members if m.get("user_id")]
        workflows_deployed = 0
        for mid in member_ids:
            wfs = rest_get(
                "workflows",
                {
                    "user_id": f"eq.{mid}",
                    "deleted_at": "is.null",
                    "select": "id",
                },
            ) or []
            workflows_deployed += len(wfs)

        clients_out.append(
            {
                "client_id": client_id,
                "email": email or "",
                "vertical": vertical,
                "tier": plan or None,
                "actions_used": actions_used,
                "actions_limit": int(ent.get("actions_limit") or 0),
                "last_active_at": last_active,
                "workflows_deployed": workflows_deployed,
                "joined_at": joined_at,
            }
        )

    clients_out.sort(key=lambda c: str(c.get("joined_at") or ""), reverse=True)

    return 200, {
        "total_clients": len(entitlements),
        "active_this_month": active_this_month,
        "total_actions_this_month": total_actions,
        "mrr_pence": mrr_pence,
        "clients": clients_out,
    }
