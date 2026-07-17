"""Blueprint conversation persistence and free-preview message counting."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from supabase_rest import (
    client_id_from_user_id,
    rest_get,
    rest_patch,
    rest_post_with_error,
)

VALID_AGENTS = frozenset({"aria", "nova", "finn", "zara", "cleo"})
FREE_PREVIEW_USER_MESSAGE_LIMIT = 5
CLAUDE_CONTEXT_MESSAGE_CAP = 20

FREE_PREVIEW_BLOCK_MESSAGE = (
    "You've reached your free preview — upgrade to keep building, "
    "deploying, and running automations."
)
WORKFLOW_UPGRADE_MESSAGE = "Upgrade to deploy and run automations."


def normalize_agent_id(agent_id: Optional[str]) -> Optional[str]:
    aid = (agent_id or "").strip().lower()
    return aid if aid in VALID_AGENTS else None


def count_user_blueprint_messages(user_id: str) -> int:
    """
    Total user-role Blueprint messages across ALL agents for this user.
    Free-preview cap is lifetime/total, not per-agent and not monthly.
    """
    uid = (user_id or "").strip()
    if not uid:
        return 0
    rows = rest_get(
        "blueprint_messages",
        {
            "user_id": f"eq.{uid}",
            "role": "eq.user",
            "select": "id",
        },
    )
    return len(rows or [])


def get_active_conversation(user_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
    uid = (user_id or "").strip()
    aid = normalize_agent_id(agent_id)
    if not uid or not aid:
        return None
    rows = rest_get(
        "blueprint_conversations",
        {
            "user_id": f"eq.{uid}",
            "agent_id": f"eq.{aid}",
            "status": "eq.active",
            "select": "*",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


def list_conversation_messages(conversation_id: str) -> List[Dict[str, Any]]:
    cid = (conversation_id or "").strip()
    if not cid:
        return []
    rows = rest_get(
        "blueprint_messages",
        {
            "conversation_id": f"eq.{cid}",
            "select": "id,conversation_id,agent_id,role,content,created_at",
            "order": "created_at.asc",
        },
    )
    return rows or []


def load_active_history(user_id: str, agent_id: str) -> Dict[str, Any]:
    """Return active conversation (if any) and its full message list for the UI."""
    aid = normalize_agent_id(agent_id)
    if not aid:
        return {"conversation": None, "messages": [], "error": "invalid_agent"}
    conversation = get_active_conversation(user_id, aid)
    if not conversation:
        return {"conversation": None, "messages": []}
    messages = list_conversation_messages(str(conversation["id"]))
    return {"conversation": conversation, "messages": messages}


def create_conversation(user_id: str, agent_id: str, client_id: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    uid = (user_id or "").strip()
    aid = normalize_agent_id(agent_id)
    if not uid or not aid:
        return None, "invalid user_id or agent_id"
    try:
        cid = (client_id or "").strip() or client_id_from_user_id(uid)
    except ValueError as exc:
        return None, str(exc)

    row, err = rest_post_with_error(
        "blueprint_conversations",
        {
            "client_id": cid,
            "user_id": uid,
            "agent_id": aid,
            "status": "active",
        },
        prefer="return=representation",
    )
    if err or not row:
        return None, err or "failed to create conversation"
    return row, ""


def get_or_create_active_conversation(
    user_id: str, agent_id: str, client_id: Optional[str] = None
) -> Tuple[Optional[Dict[str, Any]], str]:
    existing = get_active_conversation(user_id, agent_id)
    if existing:
        return existing, ""
    return create_conversation(user_id, agent_id, client_id=client_id)


def archive_conversation(conversation_id: str, user_id: str) -> bool:
    cid = (conversation_id or "").strip()
    uid = (user_id or "").strip()
    if not cid or not uid:
        return False
    return rest_patch(
        "blueprint_conversations",
        {"id": cid, "user_id": uid},
        {
            "status": "archived",
            "archived_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def start_new_conversation(user_id: str, agent_id: str) -> Tuple[int, Dict[str, Any]]:
    """Archive the active thread for this agent and open a fresh conversation."""
    aid = normalize_agent_id(agent_id)
    if not aid:
        return 400, {"detail": "agent_id must be one of aria, nova, finn, zara, cleo"}

    uid = (user_id or "").strip()
    if not uid:
        return 401, {"detail": "Authentication required", "error": "unauthenticated"}

    try:
        client_id = client_id_from_user_id(uid)
    except ValueError as exc:
        return 403, {"detail": str(exc), "error": "no_client_membership"}

    active = get_active_conversation(uid, aid)
    if active:
        archive_conversation(str(active["id"]), uid)

    row, err = create_conversation(uid, aid, client_id=client_id)
    if err or not row:
        return 502, {"detail": err or "Failed to start conversation"}
    return 200, {"conversation": row, "messages": []}


def append_message(
    *,
    conversation_id: str,
    user_id: str,
    agent_id: str,
    role: str,
    content: str,
    client_id: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], str]:
    uid = (user_id or "").strip()
    cid_conv = (conversation_id or "").strip()
    aid = normalize_agent_id(agent_id)
    r = (role or "").strip().lower()
    text = (content or "").strip()
    if not uid or not cid_conv or not aid or r not in ("user", "assistant") or not text:
        return None, "invalid message payload"
    try:
        cid = (client_id or "").strip() or client_id_from_user_id(uid)
    except ValueError as exc:
        return None, str(exc)

    row, err = rest_post_with_error(
        "blueprint_messages",
        {
            "conversation_id": cid_conv,
            "client_id": cid,
            "user_id": uid,
            "agent_id": aid,
            "role": r,
            "content": text,
        },
        prefer="return=representation",
    )
    if err or not row:
        return None, err or "failed to persist message"
    return row, ""


def cap_messages_for_claude(messages: List[Dict[str, Any]], limit: int = CLAUDE_CONTEXT_MESSAGE_CAP) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    if len(messages) <= limit:
        return list(messages)
    return list(messages[-limit:])
