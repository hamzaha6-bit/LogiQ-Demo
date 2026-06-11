"""Supabase Auth — signup, login, session validation."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from supabase_client import get_anon_key, get_client, get_url, is_configured, rest_post, rest_upsert

logger = logging.getLogger("logiq.auth")


class AuthNotConfigured(Exception):
    pass


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def public_config() -> Dict[str, Any]:
    return {
        "supabase_configured": is_configured(),
        "supabase_url": get_url() if is_configured() else "",
        "supabase_anon_key": get_anon_key() if is_configured() else "",
    }


async def signup(name: str, email: str, password: str) -> Dict[str, Any]:
    if not is_configured():
        raise AuthNotConfigured("Supabase not configured — set SUPABASE_URL and SUPABASE_SERVICE_KEY")
    client = get_client()
    if not client:
        raise AuthNotConfigured("Supabase client unavailable")

    try:
        result = client.auth.sign_up(
            {
                "email": email.strip(),
                "password": password,
                "options": {"data": {"name": name.strip()}},
            }
        )
    except Exception as exc:
        logger.exception("Signup failed")
        raise AuthError(str(exc), 400) from exc

    user = result.user
    session = result.session
    if not user:
        raise AuthError("Signup failed — check email confirmation settings", 400)

    user_id = str(user.id)
    await rest_upsert(
        "user_profiles",
        {"id": user_id, "name": name.strip() or email.split("@")[0], "plan": "starter", "onboarding_complete": False},
        on_conflict="id",
    )

    if not session:
        return {
            "user": {"id": user_id, "email": email, "name": name.strip()},
            "access_token": "",
            "message": "Check your email to confirm your account",
        }

    return _session_response(session, user_id, name.strip() or email.split("@")[0], email)


async def login(email: str, password: str) -> Dict[str, Any]:
    if not is_configured():
        raise AuthNotConfigured("Supabase not configured")
    client = get_client()
    try:
        result = client.auth.sign_in_with_password({"email": email.strip(), "password": password})
    except Exception as exc:
        logger.exception("Login failed")
        raise AuthError("Invalid email or password", 401) from exc

    if not result.session or not result.user:
        raise AuthError("Invalid email or password", 401)

    user_id = str(result.user.id)
    profile = await get_profile(user_id)
    name = profile.get("name") or result.user.user_metadata.get("name") or email.split("@")[0]
    return _session_response(result.session, user_id, name, email)


async def logout(access_token: str) -> bool:
    if not is_configured() or not access_token:
        return True
    client = get_client()
    try:
        client.auth.sign_out()
    except Exception:
        pass
    return True


async def get_user_from_token(access_token: str) -> Optional[Dict[str, Any]]:
    if not access_token or not is_configured():
        return None
    client = get_client()
    try:
        result = client.auth.get_user(access_token)
        user = result.user
        if not user:
            return None
        user_id = str(user.id)
        profile = await get_profile(user_id)
        return {
            "id": user_id,
            "email": user.email or "",
            "name": profile.get("name") or user.user_metadata.get("name") or "",
            "plan": profile.get("plan") or "starter",
            "onboarding_complete": bool(profile.get("onboarding_complete")),
        }
    except Exception as exc:
        logger.debug("Token validation failed: %s", exc)
        return None


async def get_profile(user_id: str) -> Dict[str, Any]:
    import httpx
    from supabase_client import rest_headers

    url = f"{get_url()}/rest/v1/user_profiles"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            url,
            headers=rest_headers(),
            params={"id": f"eq.{user_id}", "select": "*"},
        )
        if resp.status_code == 200:
            rows = resp.json()
            if rows:
                return rows[0]
    return {"plan": "starter", "onboarding_complete": False}


async def update_profile(user_id: str, updates: Dict[str, Any]) -> bool:
    from supabase_client import rest_patch

    return await rest_patch("user_profiles", {"id": user_id}, updates)


def _session_response(session, user_id: str, name: str, email: str) -> Dict[str, Any]:
    return {
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "expires_in": session.expires_in,
        "user": {"id": user_id, "email": email, "name": name},
    }
