"""Client-scoped monthly usage + per-user daily API/email tracking."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Dict

from supabase_rest import rest_get, rest_post


def _month_start() -> str:
    today = date.today()
    return date(today.year, today.month, 1).isoformat()


def _today() -> str:
    return date.today().isoformat()


def get_monthly_usage(client_id: str) -> Dict[str, int]:
    cid = (client_id or "").strip()
    if not cid:
        return {"actions_used": 0, "spend_pence": 0}
    rows = rest_get(
        "client_usage",
        {
            "client_id": f"eq.{cid}",
            "month": f"eq.{_month_start()}",
            "select": "actions_used,spend_pence",
        },
    )
    if rows:
        return {
            "actions_used": int(rows[0].get("actions_used") or 0),
            "spend_pence": int(rows[0].get("spend_pence") or 0),
        }
    return {"actions_used": 0, "spend_pence": 0}


def record_action(client_id: str, cost_pence: int = 0) -> None:
    cid = (client_id or "").strip()
    if not cid:
        return
    current = get_monthly_usage(cid)
    rest_post(
        "client_usage",
        {
            "client_id": cid,
            "month": _month_start(),
            "actions_used": current["actions_used"] + 1,
            "spend_pence": current["spend_pence"] + max(0, int(cost_pence)),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="client_id,month",
    )


def get_today_usage(user_id: str) -> Dict[str, int]:
    """Daily counters from usage_tracking (api_calls, emails_sent, actions_taken)."""
    uid = (user_id or "").strip()
    empty = {"api_calls": 0, "emails_sent": 0, "actions_taken": 0}
    if not uid:
        return empty
    rows = rest_get(
        "usage_tracking",
        {
            "user_id": f"eq.{uid}",
            "date": f"eq.{_today()}",
            "select": "api_calls,emails_sent,actions_taken",
        },
    )
    if not rows:
        return empty
    row = rows[0]
    return {
        "api_calls": int(row.get("api_calls") or 0),
        "emails_sent": int(row.get("emails_sent") or 0),
        "actions_taken": int(row.get("actions_taken") or 0),
    }


def _increment_daily(user_id: str, field: str, amount: int = 1) -> None:
    uid = (user_id or "").strip()
    if not uid or field not in ("api_calls", "emails_sent", "actions_taken"):
        return
    current = get_today_usage(uid)
    current[field] = int(current.get(field) or 0) + max(1, int(amount))
    rest_post(
        "usage_tracking",
        {
            "user_id": uid,
            "date": _today(),
            "api_calls": current["api_calls"],
            "emails_sent": current["emails_sent"],
            "actions_taken": current["actions_taken"],
        },
        on_conflict="user_id,date",
    )


def record_api_call(user_id: str) -> None:
    _increment_daily(user_id, "api_calls")


def record_email_sent(user_id: str) -> None:
    _increment_daily(user_id, "emails_sent")


def record_daily_action(user_id: str) -> None:
    _increment_daily(user_id, "actions_taken")
