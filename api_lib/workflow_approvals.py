"""Server-side workflow approval list / resolve (user-scoped, service role)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from supabase_rest import rest_get, rest_patch
from workflow_runner import run_workflow_for_user


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expire_stale(user_id: str) -> None:
    now = _now_iso()
    rows = rest_get(
        "workflow_approvals",
        {
            "user_id": f"eq.{user_id}",
            "status": "eq.pending",
            "expires_at": f"lte.{now}",
            "select": "id",
        },
    ) or []
    for row in rows:
        aid = str(row.get("id") or "")
        if not aid:
            continue
        rest_patch(
            "workflow_approvals",
            {"id": aid, "user_id": user_id},
            {"status": "expired", "resolved_at": now},
        )


def list_pending_approvals_for_user(user_id: str) -> Tuple[int, Dict[str, Any]]:
    uid = (user_id or "").strip()
    if not uid:
        return 401, {"detail": "Authentication required"}
    _expire_stale(uid)
    rows = rest_get(
        "workflow_approvals",
        {
            "user_id": f"eq.{uid}",
            "status": "eq.pending",
            "select": "*",
            "order": "created_at.desc",
        },
    ) or []
    return 200, {"approvals": rows}


def resolve_approval_for_user(
    user_id: str,
    approval_id: str,
    decision: str,
) -> Tuple[int, Dict[str, Any]]:
    uid = (user_id or "").strip()
    aid = (approval_id or "").strip()
    action = (decision or "").strip().lower()
    if not uid:
        return 401, {"detail": "Authentication required"}
    if not aid:
        return 400, {"detail": "approval_id is required"}
    if action not in ("approved", "rejected"):
        return 400, {"detail": "decision must be approved or rejected"}

    rows = rest_get(
        "workflow_approvals",
        {
            "id": f"eq.{aid}",
            "user_id": f"eq.{uid}",
            "select": "*",
            "limit": "1",
        },
    )
    if not rows:
        return 404, {"detail": "Approval not found"}
    row = rows[0]
    if (row.get("status") or "").lower() != "pending":
        return 409, {"detail": "Approval is not pending", "status": row.get("status")}

    now = _now_iso()
    ok = rest_patch(
        "workflow_approvals",
        {"id": aid, "user_id": uid},
        {"status": action, "resolved_at": now},
    )
    if not ok:
        return 502, {"detail": "Failed to update approval"}

    run_id = str(row.get("workflow_run_id") or "").strip()
    workflow_id = str(row.get("workflow_id") or "").strip()

    if action == "rejected":
        if run_id:
            rest_patch(
                "workflow_runs",
                {"id": run_id},
                {
                    "status": "cancelled",
                    "error": "Approval rejected",
                    "completed_at": now,
                },
            )
        return 200, {"status": "rejected", "approval_id": aid, "workflow_id": workflow_id}

    if run_id and workflow_id:
        return run_workflow_for_user(
            uid,
            workflow_id,
            workflow_run_id=run_id,
            approval_id=aid,
        )

    return 200, {"status": "approved", "approval_id": aid, "workflow_id": workflow_id}
