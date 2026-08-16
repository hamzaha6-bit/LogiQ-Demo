"""Resolve Supabase user from Vercel handler request."""
from __future__ import annotations

from typing import Optional

from supabase_rest import user_id_from_bearer


def resolve_access_token(handler) -> Optional[str]:
    """Read the session from Authorization only — never from ?token= (leaks in logs/Referer)."""
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        return token or None
    return None


def resolve_user_id(handler) -> Optional[str]:
    token = resolve_access_token(handler)
    return user_id_from_bearer(token) if token else None
