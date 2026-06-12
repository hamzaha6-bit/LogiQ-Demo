"""Audit log — governance trail."""

from __future__ import annotations

import bootstrap_path  # noqa: F401

import logging
from typing import Any, Dict, List, Optional

from supabase_client import is_configured, rest_get, rest_post

logger = logging.getLogger("logiq.audit")

# In-memory fallback when Supabase unavailable
_mem_log: List[Dict[str, Any]] = []


async def log_event(
    user_id: Optional[str],
    agent: str,
    action_type: str,
    *,
    item_id: str = "",
    recipient: str = "",
    subject: str = "",
    status: str = "completed",
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    entry = {
        "user_id": user_id,
        "agent": agent,
        "action_type": action_type,
        "item_id": item_id,
        "recipient": recipient,
        "subject": subject,
        "status": status,
        "metadata": metadata or {},
    }

    if is_configured() and user_id:
        await rest_post("audit_log", entry)
    else:
        from datetime import datetime, timezone

        entry["id"] = f"mem_{len(_mem_log)}"
        entry["created_at"] = datetime.now(timezone.utc).isoformat()
        _mem_log.insert(0, entry)
        if len(_mem_log) > 500:
            _mem_log.pop()


async def get_log(user_id: Optional[str], limit: int = 20, agent: Optional[str] = None) -> List[Dict[str, Any]]:
    if is_configured() and user_id:
        params: Dict[str, str] = {
            "user_id": f"eq.{user_id}",
            "order": "created_at.desc",
            "limit": str(limit),
            "select": "*",
        }
        if agent:
            params["agent"] = f"eq.{agent}"
        return await rest_get("audit_log", params)

    rows = _mem_log
    if user_id:
        rows = [r for r in rows if r.get("user_id") == user_id]
    if agent:
        rows = [r for r in rows if r.get("agent") == agent]
    return rows[:limit]
