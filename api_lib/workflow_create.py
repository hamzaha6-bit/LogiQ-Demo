"""Create Blueprint workflows (user_id ownership via bearer)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from supabase_rest import rest_post_with_error
from workflow_scheduler import initial_next_run, parse_schedule


def create_workflow_for_user(user_id: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """
    Insert a workflow row for the authenticated user.
    Returns (http_status, payload). On success payload is {"workflow": saved_row}.
    """
    uid = (user_id or "").strip()
    if not uid:
        return 401, {"detail": "Authentication required", "error": "unauthenticated"}

    agent_id = str(body.get("agent_id") or "").strip().lower()
    if agent_id not in ("aria", "nova"):
        return 400, {"detail": "agent_id must be aria or nova"}

    name = str(body.get("name") or "").strip() or "Custom workflow"
    description = str(body.get("description") or "").strip()
    trigger_description = str(body.get("trigger_description") or "").strip()

    steps = body.get("steps")
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except (json.JSONDecodeError, TypeError):
            return 400, {"detail": "steps must be a JSON array"}
    if not isinstance(steps, list) or not steps:
        return 400, {"detail": "steps must be a non-empty array"}

    status = str(body.get("status") or "active").strip().lower() or "active"
    if status not in ("active", "paused"):
        return 400, {"detail": "status must be active or paused"}

    schedule_raw = body.get("schedule")
    if schedule_raw is not None and schedule_raw != "":
        if isinstance(schedule_raw, dict):
            schedule_raw = json.dumps(schedule_raw)
        else:
            schedule_raw = str(schedule_raw)
        if parse_schedule(schedule_raw) is None:
            return 400, {"detail": "Invalid schedule"}
    else:
        schedule_raw = None

    now = datetime.now(timezone.utc).isoformat()
    next_at: Optional[str] = None
    sched = parse_schedule(schedule_raw)
    first = initial_next_run(sched)
    if first is not None:
        next_at = first.isoformat()

    payload: Dict[str, Any] = {
        "user_id": uid,
        "agent_id": agent_id,
        "name": name,
        "description": description,
        "trigger_description": trigger_description,
        "steps": steps,
        "status": status,
        "schedule": schedule_raw,
        "next_run_at": next_at,
        "updated_at": str(body.get("updated_at") or now),
    }

    row, err = rest_post_with_error(
        "workflows",
        payload,
        prefer="return=representation",
    )
    if not row:
        return 502, {"detail": err or "Failed to create workflow"}

    return 200, {"workflow": row}
