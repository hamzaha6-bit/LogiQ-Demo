"""Resolve Supabase user from Vercel handler request."""
from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qs, urlparse

from supabase_rest import user_id_from_bearer


def resolve_access_token(handler) -> Optional[str]:
    qs = parse_qs(urlparse(handler.path).query)
    token = (qs.get("token") or [""])[0]
    if token:
        return token.strip()
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


def resolve_user_id(handler) -> Optional[str]:
    token = resolve_access_token(handler)
    return user_id_from_bearer(token) if token else None
