"""Server-side workflow execution with execution gate and inter-step context."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from action_registry import REAL_CODES, is_real_code
from execution_gate import check_execution_gate, record_allowed_action
from google_oauth import send_user_email
from usage import record_email_sent
from sheets_service import (
    SchemaMismatchError,
    SheetsError,
    connect,
    delete_row,
    poll,
    read_sheet,
    update_row,
    write_cell,
    write_row,
)
from supabase_rest import client_id_from_user_id, rest_get, rest_patch, rest_post
from workflow_context import empty_context, resolved_params_copy, set_step_output
from workflow_scheduler import compute_next_run, parse_schedule


class StepExecutionError(Exception):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _agent_name(agent_id: str) -> str:
    if agent_id == "aria":
        return "Aria"
    if agent_id == "nova":
        return "Nova"
    return str(agent_id)


def _approval_summary(step: Dict[str, Any], params: Dict[str, Any]) -> str:
    code = step.get("code") or ""
    summary = step.get("description") or step.get("name") or code
    if code in ("GM-03", "GM-04"):
        subj = params.get("subject")
        return f"Send email{': ' + subj if subj else ''}"
    if code == "GC-06":
        title = params.get("title")
        return f"Send calendar invite{': ' + title if title else ''}"
    if code == "GC-05":
        title = params.get("title")
        return f"Cancel event{': ' + title if title else ''}"
    if code == "GS-06":
        row = params.get("row")
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
            entry["client_id"] = client_id_from_user_id(user_id)
        except ValueError:
            pass
        rest_post("audit_log", entry)
    except Exception as exc:
        print(f"[workflow_runner] audit log failed: {exc}")


def _create_approval(
    workflow: Dict[str, Any],
    step: Dict[str, Any],
    *,
    resolved_params: Dict[str, Any],
    workflow_run_id: str,
) -> None:
    user_id = str(workflow.get("user_id") or "")
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    row: Dict[str, Any] = {
        "user_id": user_id,
        "workflow_id": workflow.get("id"),
        "workflow_run_id": workflow_run_id,
        "agent_id": workflow.get("agent_id"),
        "step_number": step.get("step"),
        "primitive_code": step.get("code"),
        "action_name": step.get("name"),
        "integration": step.get("integration"),
        "summary": _approval_summary(step, resolved_params),
        "payload": resolved_params,
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


def _create_run(workflow_id: str) -> Optional[str]:
    row = rest_post(
        "workflow_runs",
        {
            "workflow_id": workflow_id,
            "status": "running",
            "context_json": {},
        },
    )
    return str(row.get("id")) if row and row.get("id") else None


def _load_run(run_id: str) -> Optional[Dict[str, Any]]:
    rows = rest_get("workflow_runs", {"id": f"eq.{run_id}", "select": "*"})
    return rows[0] if rows else None


def _save_run(
    run_id: str,
    *,
    context: Dict[str, Any],
    status: Optional[str] = None,
    error: Optional[str] = None,
    completed: bool = False,
) -> None:
    payload: Dict[str, Any] = {"context_json": context}
    if status:
        payload["status"] = status
    if error is not None:
        payload["error"] = error
    if completed:
        payload["completed_at"] = _now_iso()
    rest_patch("workflow_runs", {"id": run_id}, payload)


def _parse_steps(wf: Dict[str, Any]) -> List[Dict[str, Any]]:
    steps = wf.get("steps") or []
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except json.JSONDecodeError:
            steps = []
    return steps if isinstance(steps, list) else []


def _execute_step(
    code: str,
    params: Dict[str, Any],
    *,
    user_id: str,
    agent_id: str,
    agent_name: str,
) -> Dict[str, Any]:
    """Execute a workflow primitive. Only REAL_CODES are implemented — all others hard-fail."""
    normalized = (code or "").strip().upper()
    if not is_real_code(normalized):
        available = ", ".join(sorted(REAL_CODES))
        raise StepExecutionError(
            f"Action {normalized or '(missing)'} is not implemented. "
            f"Available actions: {available}."
        )

    if normalized.startswith("GS-"):
        return _execute_sheets_step(normalized, params, user_id=user_id, agent_id=agent_id)

    if normalized in ("GM-03", "GM-04"):
        to = (params.get("to") or "").strip()
        subject = (params.get("subject") or "").strip()
        body = (params.get("body") or "").strip()
        if not to or not subject:
            raise StepExecutionError(f"{normalized} requires to and subject")
        ok, message_id = send_user_email(user_id, to, subject, body, agent_name)
        if not ok:
            raise StepExecutionError(message_id)
        record_email_sent(user_id)
        return {"sent": True, "message_id": message_id, "to": to, "subject": subject}

    # Defense in depth: REAL_CODES must always have an explicit branch above.
    raise StepExecutionError(
        f"Action {normalized} is marked real but has no executor — refusing to continue."
    )


def _execute_sheets_step(
    code: str,
    params: Dict[str, Any],
    *,
    user_id: str,
    agent_id: str,
) -> Dict[str, Any]:
    url = (params.get("url") or params.get("sheet_url") or "").strip()
    sheet_agent = (params.get("agent") or agent_id or "aria").strip()
    try:
        if code == "GS-01":
            if not url:
                raise StepExecutionError("GS-01 requires a sheet url param")
            return read_sheet(url, sheet_agent, user_id)
        if code == "GS-02":
            if not url:
                raise StepExecutionError("GS-02 requires a sheet url param")
            row_data = params.get("row") or params.get("row_data") or params.get("data") or {}
            if not isinstance(row_data, dict) or not row_data:
                raise StepExecutionError("GS-02 requires row/row_data object")
            return write_row(url, sheet_agent, user_id, {str(k): str(v) for k, v in row_data.items()})
        if code == "GS-03":
            if not url:
                raise StepExecutionError("GS-03 requires a sheet url param")
            row_data = params.get("row_data") or params.get("data") or {}
            if not isinstance(row_data, dict) or not row_data:
                raise StepExecutionError("GS-03 requires row_data object")
            return update_row(url, sheet_agent, user_id, params.get("row"), {str(k): str(v) for k, v in row_data.items()})
        if code == "GS-04":
            if not url:
                raise StepExecutionError("GS-04 requires a sheet url param")
            return poll(url, sheet_agent, user_id)
        if code == "GS-05":
            if not url:
                raise StepExecutionError("GS-05 requires a sheet url param")
            return connect(url, sheet_agent, user_id)
        if code == "GS-06":
            if not url:
                raise StepExecutionError("GS-06 requires a sheet url param")
            return delete_row(url, sheet_agent, user_id, params.get("row"))
        if code == "GS-07":
            if not url:
                raise StepExecutionError("GS-07 requires a sheet url param")
            return write_cell(url, sheet_agent, user_id, params.get("cell") or "", params.get("value"))
    except StepExecutionError:
        raise
    except SchemaMismatchError as exc:
        raise StepExecutionError(str(exc)) from exc
    except SheetsError as exc:
        raise StepExecutionError(str(exc)) from exc
    except Exception as exc:
        raise StepExecutionError(f"{code} failed: {exc}") from exc

    raise StepExecutionError(f"Unhandled Sheets action {code}")


def _finish_workflow_schedule(wf: Dict[str, Any], wid: str) -> None:
    schedule = parse_schedule(wf.get("schedule"))
    next_at = compute_next_run(schedule, datetime.now(timezone.utc))
    next_iso = next_at.isoformat() if next_at else None
    _update_workflow_run_times(wid, last_run_at=_now_iso(), next_run_at=next_iso)


def _run_steps(
    *,
    wf: Dict[str, Any],
    steps: List[Dict[str, Any]],
    uid: str,
    wid: str,
    run_id: str,
    context: Dict[str, Any],
    agent_id: str,
    agent_name: str,
    start_after_step: int = 0,
    include_start_step: bool = False,
) -> Tuple[int, Dict[str, Any]]:
    for step in steps:
        step_num = int(step.get("step") or 0)
        if include_start_step:
            if step_num < start_after_step:
                continue
        elif step_num <= start_after_step:
            continue

        resolved = resolved_params_copy(step.get("params") or {}, context)

        if step.get("requires_approval"):
            _create_approval(
                wf,
                step,
                resolved_params=resolved,
                workflow_run_id=run_id,
            )
            _log_audit(
                uid,
                agent_name,
                "workflow_paused",
                {
                    "workflow_id": wid,
                    "workflow_run_id": run_id,
                    "step": step_num,
                    "code": step.get("code"),
                    "status": "pending",
                },
            )
            _save_run(run_id, context=context, status="paused")
            return 200, {
                "status": "pending_approval",
                "workflow_id": wid,
                "workflow_run_id": run_id,
                "step": step_num,
                "code": step.get("code"),
            }

        try:
            output = _execute_step(
                step.get("code") or "",
                resolved,
                user_id=uid,
                agent_id=agent_id,
                agent_name=agent_name,
            )
        except StepExecutionError as exc:
            _save_run(run_id, context=context, status="failed", error=str(exc))
            return 500, {
                "status": "failed",
                "workflow_id": wid,
                "workflow_run_id": run_id,
                "step": step_num,
                "detail": str(exc),
            }

        set_step_output(context, step_num, output)
        _save_run(run_id, context=context, status="running")
        _log_audit(
            uid,
            agent_name,
            "workflow_step",
            {
                "workflow_id": wid,
                "workflow_run_id": run_id,
                "step": step_num,
                "code": step.get("code"),
                "status": "completed",
            },
        )

    _save_run(run_id, context=context, status="completed", completed=True)
    _finish_workflow_schedule(wf, wid)
    return 200, {
        "status": "completed",
        "workflow_id": wid,
        "workflow_run_id": run_id,
    }


def run_workflow_for_user(
    user_id: str,
    workflow_id: str,
    *,
    workflow_run_id: Optional[str] = None,
    approval_id: Optional[str] = None,
) -> Tuple[int, Dict[str, Any]]:
    """
    Execute or resume a workflow. Returns (http_status, payload).
    Fresh run: workflow_id only (gate-checked).
    Resume after approval: workflow_id + workflow_run_id + approval_id.
    """
    uid = (user_id or "").strip()
    wid = (workflow_id or "").strip()
    if not uid or not wid:
        return 400, {"detail": "user_id and workflow_id are required"}

    rows = rest_get(
        "workflows",
        {
            "id": f"eq.{wid}",
            "user_id": f"eq.{uid}",
            "deleted_at": "is.null",
            "select": "*",
        },
    )
    if not rows:
        return 404, {"detail": "Workflow not found"}

    wf = rows[0]
    if (wf.get("status") or "").lower() != "active":
        return 409, {"detail": "Workflow is not active", "status": wf.get("status")}

    steps = _parse_steps(wf)
    agent_id = wf.get("agent_id") or "aria"
    agent_name = _agent_name(agent_id)

    if workflow_run_id and approval_id:
        return _resume_after_approval(
            uid=uid,
            wid=wid,
            wf=wf,
            steps=steps,
            agent_id=agent_id,
            agent_name=agent_name,
            workflow_run_id=workflow_run_id.strip(),
            approval_id=approval_id.strip(),
        )

    gate = check_execution_gate(uid, "workflow_run")
    if not gate.allowed:
        return 403, gate.as_error_payload()

    run_id = _create_run(wid)
    if not run_id:
        return 502, {"detail": "Failed to create workflow run"}

    record_allowed_action(gate.client_id, "workflow_run")

    context = empty_context()
    status, payload = _run_steps(
        wf=wf,
        steps=steps,
        uid=uid,
        wid=wid,
        run_id=run_id,
        context=context,
        agent_id=agent_id,
        agent_name=agent_name,
        start_after_step=0,
    )
    return status, payload


def _resume_after_approval(
    *,
    uid: str,
    wid: str,
    wf: Dict[str, Any],
    steps: List[Dict[str, Any]],
    agent_id: str,
    agent_name: str,
    workflow_run_id: str,
    approval_id: str,
) -> Tuple[int, Dict[str, Any]]:
    run = _load_run(workflow_run_id)
    if not run or str(run.get("workflow_id")) != wid:
        return 404, {"detail": "Workflow run not found"}

    if (run.get("status") or "").lower() != "paused":
        return 409, {"detail": "Workflow run is not paused", "status": run.get("status")}

    approval_rows = rest_get(
        "workflow_approvals",
        {
            "id": f"eq.{approval_id}",
            "user_id": f"eq.{uid}",
            "workflow_id": f"eq.{wid}",
            "select": "*",
        },
    )
    if not approval_rows:
        return 404, {"detail": "Approval not found"}

    approval = approval_rows[0]
    if (approval.get("status") or "").lower() != "approved":
        return 409, {"detail": "Approval must be approved before resume", "status": approval.get("status")}

    step_num = int(approval.get("step_number") or 0)
    step = next((s for s in steps if int(s.get("step") or 0) == step_num), None)
    if not step:
        return 404, {"detail": f"Step {step_num} not found in workflow"}

    context = run.get("context_json") or {}
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except json.JSONDecodeError:
            context = {}

    resolved = approval.get("payload") or {}
    if isinstance(resolved, str):
        try:
            resolved = json.loads(resolved)
        except json.JSONDecodeError:
            resolved = {}

    _save_run(workflow_run_id, context=context, status="running")

    try:
        output = _execute_step(
            step.get("code") or "",
            resolved,
            user_id=uid,
            agent_id=agent_id,
            agent_name=agent_name,
        )
    except StepExecutionError as exc:
        _save_run(workflow_run_id, context=context, status="failed", error=str(exc))
        return 500, {
            "status": "failed",
            "workflow_id": wid,
            "workflow_run_id": workflow_run_id,
            "step": step_num,
            "detail": str(exc),
        }

    set_step_output(context, step_num, output)
    _save_run(workflow_run_id, context=context, status="running")
    _log_audit(
        uid,
        agent_name,
        "approval_granted",
        {
            "workflow_id": wid,
            "workflow_run_id": workflow_run_id,
            "approval_id": approval_id,
            "step": step_num,
            "code": step.get("code"),
            "status": "completed",
        },
    )

    return _run_steps(
        wf=wf,
        steps=steps,
        uid=uid,
        wid=wid,
        run_id=workflow_run_id,
        context=context,
        agent_id=agent_id,
        agent_name=agent_name,
        start_after_step=step_num,
    )


def run_due_scheduled_workflows() -> Dict[str, Any]:
    """Find active workflows with next_run_at <= now and run each."""
    now = datetime.now(timezone.utc).isoformat()
    due = rest_get(
        "workflows",
        {
            "status": "eq.active",
            "deleted_at": "is.null",
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
