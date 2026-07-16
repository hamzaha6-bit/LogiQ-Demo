"""Workflow list / latest-run queries (service role)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from supabase_rest import rest_get


def _latest_run_for_workflow(workflow_id: str) -> Optional[Dict[str, Any]]:
    wid = (workflow_id or "").strip()
    if not wid:
        return None
    rows = rest_get(
        "workflow_runs",
        {
            "workflow_id": f"eq.{wid}",
            "select": "id,status,started_at,completed_at,context_json,error",
            "order": "started_at.desc",
            "limit": "1",
        },
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "id": row.get("id"),
        "status": row.get("status"),
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "context_json": row.get("context_json") or {},
        "error": row.get("error"),
    }


def list_workflows_for_user(user_id: str) -> Tuple[int, Dict[str, Any]]:
    uid = (user_id or "").strip()
    if not uid:
        return 401, {"detail": "Authentication required"}

    rows = rest_get(
        "workflows",
        {
            "user_id": f"eq.{uid}",
            "deleted_at": "is.null",
            "select": "id,name,agent_id,description,trigger_description,schedule,status,next_run_at,last_run_at,steps,created_at,updated_at",
            "order": "created_at.desc",
        },
    ) or []

    workflows: List[Dict[str, Any]] = []
    for wf in rows:
        item = dict(wf)
        item["last_run"] = _latest_run_for_workflow(str(wf.get("id") or ""))
        workflows.append(item)

    return 200, {"workflows": workflows}


def latest_run_for_user_workflow(user_id: str, workflow_id: str) -> Tuple[int, Dict[str, Any]]:
    uid = (user_id or "").strip()
    wid = (workflow_id or "").strip()
    if not uid:
        return 401, {"detail": "Authentication required"}
    if not wid:
        return 400, {"detail": "workflow_id is required"}

    owned = rest_get(
        "workflows",
        {
            "id": f"eq.{wid}",
            "user_id": f"eq.{uid}",
            "deleted_at": "is.null",
            "select": "id",
            "limit": "1",
        },
    )
    if not owned:
        return 404, {"detail": "Workflow not found"}

    return 200, {"run": _latest_run_for_workflow(wid)}
