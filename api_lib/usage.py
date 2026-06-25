"""Client-scoped monthly usage tracking for the execution gate."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict

from supabase_rest import rest_get, rest_post


def _month_start() -> str:
    today = date.today()
    return date(today.year, today.month, 1).isoformat()


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
