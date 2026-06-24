"""Stripe billing integration."""

from __future__ import annotations

import bootstrap_path  # noqa: F401

import logging
import os
from typing import Any, Dict, Optional

import auth_service
from supabase_client import is_configured

logger = logging.getLogger("logiq.billing")

PLANS = {
    "starter": {
        "name": "Starter",
        "price_gbp": 49,
        "max_agents": 3,
        "max_actions_month": 5000,
        "max_api_calls_day": 100,
        "max_emails_day": 50,
        "env_price": "STRIPE_STARTER_PRICE_ID",
    },
    "pro": {
        "name": "Pro",
        "price_gbp": 149,
        "max_agents": None,
        "max_actions_month": 25000,
        "max_api_calls_day": 500,
        "max_emails_day": 500,
        "env_price": "STRIPE_PRO_PRICE_ID",
    },
    "business": {
        "name": "Business",
        "price_gbp": 399,
        "max_agents": None,
        "max_actions_month": None,
        "max_api_calls_day": None,
        "max_emails_day": None,
        "env_price": "STRIPE_BUSINESS_PRICE_ID",
    },
}


def is_configured() -> bool:
    return bool((os.getenv("STRIPE_SECRET_KEY") or "").strip())


def _stripe():
    import stripe

    stripe.api_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    return stripe


def get_price_id(plan: str) -> Optional[str]:
    cfg = PLANS.get(plan.lower())
    if not cfg:
        return None
    return (os.getenv(cfg["env_price"]) or "").strip() or None


def get_plan_limits(plan: str) -> Dict[str, Any]:
    return PLANS.get(plan.lower(), PLANS["starter"])


async def get_billing_status(user_id: str) -> Dict[str, Any]:
    profile = await auth_service.get_profile(user_id)
    plan = profile.get("plan") or "starter"
    limits = get_plan_limits(plan)

    from usage import get_month_usage, get_today_usage

    today = await get_today_usage(user_id)
    month = await get_month_usage(user_id)

    def pct(used, limit):
        if limit is None:
            return 0
        return min(100, round((used / limit) * 100)) if limit else 0

    api_limit = limits.get("max_api_calls_day")
    email_limit = limits.get("max_emails_day")
    actions_limit = limits.get("max_actions_month")

    return {
        "plan": plan,
        "plan_name": limits["name"],
        "limits": {
            "max_agents": limits["max_agents"],
            "max_actions_month": actions_limit,
            "max_api_calls_day": api_limit,
            "max_emails_day": email_limit,
        },
        "usage": {
            "api_calls_today": today.get("api_calls", 0),
            "emails_sent_today": today.get("emails_sent", 0),
            "actions_this_month": month.get("actions_taken", 0),
        },
        "percentages": {
            "api_calls": pct(today.get("api_calls", 0), api_limit),
            "emails": pct(today.get("emails_sent", 0), email_limit),
            "actions": pct(month.get("actions_taken", 0), actions_limit),
        },
        "stripe_configured": is_configured(),
    }
