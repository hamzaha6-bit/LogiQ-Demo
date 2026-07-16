"""Soft-delete Blueprint workflows (user_id ownership only)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from supabase_rest import rest_get, rest_patch


def soft_delete_workflow_for_user(user_id: str, workflow_id: str) -> Tuple[int, Dict[str, Any]]:
    """
    Set deleted_at = now() and status = 'deleted'.
    Ownership: workflow.user_id must equal caller (no client_id check).
    """
    uid = (user_id or "").strip()
    wid = (workflow_id or "").strip()
    if not uid:
        return 401, {"detail": "Authentication required", "error": "unauthenticated"}
    if not wid:
        return 400, {"detail": "workflow_id is required"}

    rows = rest_get(
        "workflows",
        {
            "id": f"eq.{wid}",
            "user_id": f"eq.{uid}",
            "deleted_at": "is.null",
            "select": "id,status,deleted_at",
        },
    )
    if not rows:
        # Already deleted or not owned — treat missing as 404
        any_owned = rest_get(
            "workflows",
            {"id": f"eq.{wid}", "user_id": f"eq.{uid}", "select": "id,status,deleted_at"},
        )
        if any_owned and any_owned[0].get("deleted_at"):
            return 200, {"deleted": True, "workflow_id": wid, "already_deleted": True}
        return 404, {"detail": "Workflow not found"}

    now = datetime.now(timezone.utc).isoformat()
    ok = rest_patch(
        "workflows",
        {"id": wid},
        {"deleted_at": now, "status": "deleted", "updated_at": now},
    )
    if not ok:
        return 502, {"detail": "Failed to delete workflow"}

    return 200, {"deleted": True, "workflow_id": wid}
