"""Billing status for the dashboard — entitlements + client_usage."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from entitlements import get_entitlement
from supabase_rest import client_id_from_user_id
from usage import get_monthly_usage


def _stripe_configured() -> bool:
    return bool((os.environ.get("STRIPE_SECRET_KEY") or "").strip())


def _pct(used: int, limit: int) -> int:
    if limit <= 0:
        return 0
    return min(100, round((used / limit) * 100))


def _title_plan_name(plan_slug: str) -> str:
    return (plan_slug or "inactive").strip().title()


def _inactive_status() -> Dict[str, Any]:
    return {
        "plan": "inactive",
        "plan_name": "Inactive",
        "status": "inactive",
        "usage": {
            "actions_this_month": 0,
            "api_calls_today": 0,
            "emails_sent_today": 0,
            "api_calls": 0,
            "emails_sent": 0,
        },
        "limits": {
            "max_actions_month": 0,
            "max_api_calls_day": 0,
            "max_emails_day": 0,
            "max_agents": 0,
            "max_workflows": 0,
        },
        "percentages": {
            "actions": 0,
            "api_calls": 0,
            "emails": 0,
        },
        "spend": {
            "used_pence": 0,
            "cap_pence": 0,
            "percentage": 0,
        },
        "stripe_configured": _stripe_configured(),
    }


def get_billing_status(user_id: str) -> Dict[str, Any]:
    try:
        client_id = client_id_from_user_id(user_id)
    except ValueError:
        return _inactive_status()

    entitlement = get_entitlement(client_id)
    if not entitlement:
        return _inactive_status()

    status = (entitlement.get("status") or "").strip().lower()
    if status != "active":
        return _inactive_status()

    usage = get_monthly_usage(client_id)
    actions_used = int(usage.get("actions_used") or 0)
    spend_used = int(usage.get("spend_pence") or 0)

    plan = (entitlement.get("plan") or "starter").strip().lower()
    actions_limit = int(entitlement.get("actions_limit") or 0)
    agents_limit = int(entitlement.get("agents_limit") or 0)
    workflows_limit = int(entitlement.get("workflows_limit") or 0)
    spend_cap = int(entitlement.get("spend_cap_pence") or 0)

    return {
        "plan": plan,
        "plan_name": _title_plan_name(plan),
        "status": status,
        "usage": {
            "actions_this_month": actions_used,
            "api_calls_today": 0,
            "emails_sent_today": 0,
            "api_calls": 0,
            "emails_sent": 0,
        },
        "limits": {
            "max_actions_month": actions_limit,
            "max_api_calls_day": 0,
            "max_emails_day": 0,
            "max_agents": agents_limit,
            "max_workflows": workflows_limit,
        },
        "percentages": {
            "actions": _pct(actions_used, actions_limit),
            "api_calls": 0,
            "emails": 0,
        },
        "spend": {
            "used_pence": spend_used,
            "cap_pence": spend_cap,
            "percentage": _pct(spend_used, spend_cap),
        },
        "stripe_configured": _stripe_configured(),
    }


def billing_status_for_request(user_id: Optional[str]) -> Tuple[int, Dict[str, Any]]:
    if not user_id:
        return 401, {"detail": "Valid Bearer token required"}
    return 200, get_billing_status(user_id)
