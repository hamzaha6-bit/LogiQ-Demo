"""Per-user usage tracking and plan limit enforcement."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional, Tuple

import auth_service
from billing import get_plan_limits
from supabase_client import is_configured, rest_get, rest_post

logger = logging.getLogger("logiq.usage")

_mem_usage: Dict[str, Dict[str, Dict[str, int]]] = {}


def _today() -> str:
    return date.today().isoformat()


def _month_start() -> str:
    today = date.today()
    return date(today.year, today.month, 1).isoformat()


async def _get_row(user_id: str, day: str) -> Dict[str, int]:
    if is_configured():
        rows = await rest_get(
            "usage_tracking",
            {
                "user_id": f"eq.{user_id}",
                "date": f"eq.{day}",
                "select": "api_calls,emails_sent,actions_taken",
            },
        )
        if rows:
            return {
                "api_calls": rows[0].get("api_calls") or 0,
                "emails_sent": rows[0].get("emails_sent") or 0,
                "actions_taken": rows[0].get("actions_taken") or 0,
            }
        return {"api_calls": 0, "emails_sent": 0, "actions_taken": 0}

    bucket = _mem_usage.setdefault(user_id, {}).setdefault(day, {"api_calls": 0, "emails_sent": 0, "actions_taken": 0})
    return dict(bucket)


async def _increment(user_id: str, field: str, amount: int = 1) -> None:
    day = _today()
    current = await _get_row(user_id, day)
    current[field] = current.get(field, 0) + amount

    if is_configured():
        await rest_post(
            "usage_tracking",
            {
                "user_id": user_id,
                "date": day,
                "api_calls": current["api_calls"],
                "emails_sent": current["emails_sent"],
                "actions_taken": current["actions_taken"],
            },
        )
    else:
        _mem_usage.setdefault(user_id, {})[day] = current


async def get_today_usage(user_id: str) -> Dict[str, int]:
    return await _get_row(user_id, _today())


async def get_month_usage(user_id: str) -> Dict[str, int]:
    if is_configured():
        import httpx
        from supabase_client import get_url, rest_headers

        url = f"{get_url()}/rest/v1/usage_tracking"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                headers=rest_headers(),
                params={
                    "user_id": f"eq.{user_id}",
                    "date": f"gte.{_month_start()}",
                    "select": "api_calls,emails_sent,actions_taken",
                },
            )
            if resp.status_code == 200:
                rows = resp.json()
                totals = {"api_calls": 0, "emails_sent": 0, "actions_taken": 0}
                for row in rows:
                    totals["api_calls"] += row.get("api_calls") or 0
                    totals["emails_sent"] += row.get("emails_sent") or 0
                    totals["actions_taken"] += row.get("actions_taken") or 0
                return totals

    totals = {"api_calls": 0, "emails_sent": 0, "actions_taken": 0}
    month_prefix = _month_start()[:7]
    for day, row in _mem_usage.get(user_id, {}).items():
        if day.startswith(month_prefix):
            for k in totals:
                totals[k] += row.get(k, 0)
    return totals


async def check_api_limit(user_id: str) -> Tuple[bool, str]:
    profile = await auth_service.get_profile(user_id)
    plan = profile.get("plan") or "starter"
    limits = get_plan_limits(plan)
    daily_limit = limits.get("max_api_calls_day")
    if daily_limit is None:
        return True, ""
    usage = await get_today_usage(user_id)
    if usage.get("api_calls", 0) >= daily_limit:
        return False, "Daily limit reached — upgrade your plan"
    return True, ""


async def check_email_limit(user_id: str) -> Tuple[bool, str]:
    profile = await auth_service.get_profile(user_id)
    plan = profile.get("plan") or "starter"
    limits = get_plan_limits(plan)
    daily_limit = limits.get("max_emails_day")
    if daily_limit is None:
        return True, ""
    usage = await get_today_usage(user_id)
    if usage.get("emails_sent", 0) >= daily_limit:
        return False, "Daily limit reached — upgrade your plan"
    return True, ""


async def record_api_call(user_id: str) -> None:
    await _increment(user_id, "api_calls")


async def record_email_sent(user_id: str) -> None:
    await _increment(user_id, "emails_sent")


async def record_action(user_id: str) -> None:
    await _increment(user_id, "actions_taken")
