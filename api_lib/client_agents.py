"""Client-scoped agent activation (tier active-agent limits)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from entitlements import get_entitlement
from supabase_rest import client_id_from_user_id, rest_get, rest_patch_filter, rest_post
from tiers import limits_for, upgrade_tier_for_agents


def count_active_agents(client_id: str) -> int:
    cid = (client_id or "").strip()
    if not cid:
        return 0
    rows = rest_get(
        "client_agents",
        {
            "client_id": f"eq.{cid}",
            "status": "eq.active",
            "select": "id",
        },
    )
    return len(rows or [])


def list_client_agents(client_id: str) -> List[Dict[str, Any]]:
    cid = (client_id or "").strip()
    if not cid:
        return []
    return rest_get(
        "client_agents",
        {
            "client_id": f"eq.{cid}",
            "select": "agent_id,status,activated_at,updated_at",
            "order": "activated_at.asc",
        },
    ) or []


def is_agent_active(client_id: str, agent_id: str) -> bool:
    cid = (client_id or "").strip()
    aid = (agent_id or "").strip().lower()
    if not cid or not aid:
        return False
    rows = rest_get(
        "client_agents",
        {
            "client_id": f"eq.{cid}",
            "agent_id": f"eq.{aid}",
            "status": "eq.active",
            "select": "id",
            "limit": "1",
        },
    )
    return bool(rows)


def _limit_from_entitlement(entitlement: Dict[str, Any]) -> int:
    """agents_limit from entitlements; 0 = unlimited when subscription is active."""
    return int(entitlement.get("agents_limit") or 0)


def activate_agent_for_user(user_id: str, agent_id: str) -> Tuple[int, Dict[str, Any]]:
    """
    Activate an agent for the user's client if under tier limit.
    Returns (http_status, payload).
    """
    uid = (user_id or "").strip()
    aid = (agent_id or "").strip().lower()
    if not uid:
        return 401, {"detail": "Authentication required", "error": "unauthenticated"}
    if not aid:
        return 400, {"detail": "agent_id is required"}

    try:
        client_id = client_id_from_user_id(uid)
    except ValueError as exc:
        return 403, {"detail": str(exc), "error": "no_client_membership"}

    entitlement = get_entitlement(client_id)
    if not entitlement or (entitlement.get("status") or "").strip().lower() != "active":
        return 402, {
            "detail": "Please subscribe to continue using LogiQ.",
            "error": "no_active_subscription",
        }

    limit = _limit_from_entitlement(entitlement)
    plan = (entitlement.get("plan") or "").strip().lower()

    if is_agent_active(client_id, aid):
        return 200, {
            "activated": True,
            "agent_id": aid,
            "already_active": True,
            "active_count": count_active_agents(client_id),
            "limit": limit,
            "plan": plan,
        }

    active_count = count_active_agents(client_id)
    # limit > 0 and already at capacity → refuse. limit == 0 → unlimited.
    if limit > 0 and active_count >= limit:
        upgrade = upgrade_tier_for_agents(plan) or "pro"
        upgrade_limit = int(limits_for(upgrade).get("active_agents_limit") or limits_for(upgrade).get("agents") or 0)
        return 403, {
            "error": "agent_limit_reached",
            "detail": (
                f"Your {plan.title() or 'current'} plan allows {limit} active agent"
                f"{'s' if limit != 1 else ''}. Upgrade to {upgrade.title()} "
                f"({upgrade_limit if upgrade_limit else 'unlimited'} agents) to activate more."
            ),
            "message": (
                f"Your plan allows {limit} active agent{'s' if limit != 1 else ''}. "
                f"Upgrade to {upgrade.title()} to activate more."
            ),
            "plan": plan,
            "limit": limit,
            "active_count": active_count,
            "upgrade_tier": upgrade,
            "upgrade_limit": upgrade_limit,
        }

    now = datetime.now(timezone.utc).isoformat()
    row = rest_post(
        "client_agents",
        {
            "client_id": client_id,
            "agent_id": aid,
            "status": "active",
            "activated_at": now,
            "updated_at": now,
        },
        on_conflict="client_id,agent_id",
    )
    if not row:
        # Upsert may return via merge — ensure status active
        rest_patch_filter(
            "client_agents",
            {"client_id": f"eq.{client_id}", "agent_id": f"eq.{aid}"},
            {"status": "active", "updated_at": now},
        )

    return 200, {
        "activated": True,
        "agent_id": aid,
        "already_active": False,
        "active_count": count_active_agents(client_id),
        "limit": limit,
        "plan": plan,
    }


def pause_agent_for_user(user_id: str, agent_id: str) -> Tuple[int, Dict[str, Any]]:
    uid = (user_id or "").strip()
    aid = (agent_id or "").strip().lower()
    if not uid:
        return 401, {"detail": "Authentication required", "error": "unauthenticated"}
    if not aid:
        return 400, {"detail": "agent_id is required"}

    try:
        client_id = client_id_from_user_id(uid)
    except ValueError as exc:
        return 403, {"detail": str(exc), "error": "no_client_membership"}

    now = datetime.now(timezone.utc).isoformat()
    rest_patch_filter(
        "client_agents",
        {"client_id": f"eq.{client_id}", "agent_id": f"eq.{aid}"},
        {"status": "paused", "updated_at": now},
    )
    return 200, {
        "paused": True,
        "agent_id": aid,
        "active_count": count_active_agents(client_id),
    }


def agents_status_for_user(user_id: str) -> Tuple[int, Dict[str, Any]]:
    uid = (user_id or "").strip()
    if not uid:
        return 401, {"detail": "Authentication required", "error": "unauthenticated"}
    try:
        client_id = client_id_from_user_id(uid)
    except ValueError as exc:
        return 403, {"detail": str(exc), "error": "no_client_membership"}

    entitlement = get_entitlement(client_id) or {}
    status = (entitlement.get("status") or "").strip().lower()
    plan = (entitlement.get("plan") or "").strip().lower() if status == "active" else ""
    limit = _limit_from_entitlement(entitlement) if status == "active" else 0
    agents = list_client_agents(client_id)
    active = [a for a in agents if (a.get("status") or "").lower() == "active"]
    return 200, {
        "plan": plan or None,
        "limit": limit,
        "active_count": len(active),
        "agents": agents,
        "active_agent_ids": [str(a.get("agent_id") or "") for a in active],
        "upgrade_tier": upgrade_tier_for_agents(plan) if plan else None,
    }
