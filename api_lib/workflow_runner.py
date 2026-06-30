"""Server-side workflow execution with execution gate."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from execution_gate import check_execution_gate, record_allowed_action
from supabase_rest import client_id_from_user_id, rest_get, rest_patch, rest_post
from workflow_scheduler import compute_next_run, parse_schedule


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _approval_summary(step: Dict[str, Any]) -> str:
    code = step.get("code") or ""
    payload = step.get("params") or {}
    summary = step.get("description") or step.get("name") or code
    if code in ("GM-03", "GM-04"):
        subj = payload.get("subject")
        return f"Send email{': ' + subj if subj else ''}"
    if code == "GC-06":
        title = payload.get("title")
        return f"Send calendar invite{': ' + title if title else ''}"
    if code == "GC-05":
        title = payload.get("title")
        return f"Cancel event{': ' + title if title else ''}"
    if code == "GS-06":
        row = payload.get("row")
        return f"Delete sheet row{' #' + str(row) if row else ''}"
    return summary


def _log_audit(user_id: str, agent: str, action_type: str, metadata: Dict[str, Any]) -> None:
    try:
        entry: Dict[str, Any] = {
            "user_id": user_id,
            "agent": agent,
            "action_type": action_type,
            "status": metadata.get("status", "completed"),
            "metadata": metadata,
        }
        try:
            cid = client_id_from_user_id(user_id)
            entry["client_id"] = cid
        except ValueError:
            pass
        rest_post("audit_log", entry)
    except Exception as exc:
        print(f"[workflow_runner] audit log failed: {exc}")


def _create_approval(workflow: Dict[str, Any], step: Dict[str, Any]) -> None:
    user_id = str(workflow.get("user_id") or "")
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    row: Dict[str, Any] = {
        "user_id": user_id,
        "workflow_id": workflow.get("id"),
        "agent_id": workflow.get("agent_id"),
        "step_number": step.get("step"),
        "primitive_code": step.get("code"),
        "action_name": step.get("name"),
        "integration": step.get("integration"),
        "summary": _approval_summary(step),
        "payload": step.get("params") or {},
        "status": "pending",
        "expires_at": expires,
    }
    try:
        row["client_id"] = client_id_from_user_id(user_id)
    except ValueError:
        pass
    rest_post("workflow_approvals", row)


def _update_workflow_run_times(
    workflow_id: str,
    *,
    last_run_at: str,
    next_run_at: Optional[str],
) -> None:
    payload: Dict[str, Any] = {
        "last_run_at": last_run_at,
        "updated_at": last_run_at,
    }
    if next_run_at is not None:
        payload["next_run_at"] = next_run_at
    rest_patch("workflows", {"id": workflow_id}, payload)


def run_workflow_for_user(user_id: str, workflow_id: str) -> Tuple[int, Dict[str, Any]]:
    """
    Execute workflow steps server-side. Returns (http_status, payload).
    Mirrors index.html runWorkflow + gate check.
    """
    uid = (user_id or "").strip()
    wid = (workflow_id or "").strip()
    if not uid or not wid:
        return 400, {"detail": "user_id and workflow_id are required"}

    rows = rest_get(
        "workflows",
        {"id": f"eq.{wid}", "user_id": f"eq.{uid}", "select": "*"},
    )
    if not rows:
        return 404, {"detail": "Workflow not found"}

    wf = rows[0]
    if (wf.get("status") or "").lower() != "active":
        return 409, {"detail": "Workflow is not active", "status": wf.get("status")}

    gate = check_execution_gate(uid, "workflow_run")
    if not gate.allowed:
        return 403, gate.as_error_payload()

    steps = wf.get("steps") or []
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except json.JSONDecodeError:
            steps = []

    agent_id = wf.get("agent_id") or "aria"
    agent_name = "Aria" if agent_id == "aria" else "Nova" if agent_id == "nova" else str(agent_id)

    for step in steps:
        if step.get("requires_approval"):
            _create_approval(wf, step)
            _log_audit(
                uid,
                agent_name,
                "workflow_paused",
                {
                    "workflow_id": wid,
                    "step": step.get("step"),
                    "code": step.get("code"),
                    "status": "pending",
                },
            )
            schedule = parse_schedule(wf.get("schedule"))
            next_at = compute_next_run(schedule, datetime.now(timezone.utc))
            next_iso = next_at.isoformat() if next_at else None
            _update_workflow_run_times(wid, last_run_at=_now_iso(), next_run_at=next_iso)
            record_allowed_action(gate.client_id, "workflow_run")
            return 200, {
                "status": "pending_approval",
                "workflow_id": wid,
                "step": step.get("step"),
                "code": step.get("code"),
            }

        _log_audit(
            uid,
            agent_name,
            "workflow_step",
            {
                "workflow_id": wid,
                "step": step.get("step"),
                "code": step.get("code"),
                "status": "completed",
            },
        )

    schedule = parse_schedule(wf.get("schedule"))
    next_at = compute_next_run(schedule, datetime.now(timezone.utc))
    next_iso = next_at.isoformat() if next_at else None
    _update_workflow_run_times(wid, last_run_at=_now_iso(), next_run_at=next_iso)
    record_allowed_action(gate.client_id, "workflow_run")
    return 200, {"status": "completed", "workflow_id": wid}


def run_due_scheduled_workflows() -> Dict[str, Any]:
    """Find active workflows with next_run_at <= now and run each."""
    now = datetime.now(timezone.utc).isoformat()
    due = rest_get(
        "workflows",
        {
            "status": "eq.active",
            "schedule": "not.is.null",
            "next_run_at": f"lte.{now}",
            "select": "id,user_id,name,schedule",
        },
    )
    results: List[Dict[str, Any]] = []
    for wf in due:
        wid = str(wf.get("id") or "")
        uid = str(wf.get("user_id") or "")
        if not wid or not uid:
            continue
        status, payload = run_workflow_for_user(uid, wid)
        results.append(
            {
                "workflow_id": wid,
                "user_id": uid,
                "name": wf.get("name"),
                "http_status": status,
                "result": payload,
            }
        )
    return {"ran": len(results), "workflows": results}
