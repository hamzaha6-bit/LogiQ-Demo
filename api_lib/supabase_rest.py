"""Shared Supabase REST helpers for Vercel API functions."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx


def env(key: str) -> str:
    return (os.environ.get(key) or "").strip()


def rest_headers(prefer: str = "return=representation") -> Dict[str, str]:
    key = env("SUPABASE_SERVICE_KEY")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def rest_get(table: str, params: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    url = f"{env('SUPABASE_URL').rstrip('/')}/rest/v1/{table}"
    if not env("SUPABASE_URL") or not env("SUPABASE_SERVICE_KEY"):
        return []
    with httpx.Client(timeout=20) as client:
        resp = client.get(url, headers=rest_headers("return=representation"), params=params or {})
        if resp.status_code >= 400:
            return []
        data = resp.json()
        return data if isinstance(data, list) else []


def rest_post(
    table: str,
    payload: Dict[str, Any],
    *,
    on_conflict: str = "",
    prefer: str = "resolution=merge-duplicates,return=representation",
) -> Optional[Dict[str, Any]]:
    row, _err = rest_post_with_error(table, payload, on_conflict=on_conflict, prefer=prefer)
    return row


def rest_post_with_error(
    table: str,
    payload: Dict[str, Any],
    *,
    on_conflict: str = "",
    prefer: str = "resolution=merge-duplicates,return=representation",
) -> tuple[Optional[Dict[str, Any]], str]:
    url = f"{env('SUPABASE_URL').rstrip('/')}/rest/v1/{table}"
    if not env("SUPABASE_URL") or not env("SUPABASE_SERVICE_KEY"):
        return None, "SUPABASE_URL or SUPABASE_SERVICE_KEY not configured"
    params = {"on_conflict": on_conflict} if on_conflict else None
    with httpx.Client(timeout=20) as client:
        resp = client.post(url, headers=rest_headers(prefer), params=params, json=payload)
        if resp.status_code >= 400:
            err = f"HTTP {resp.status_code}: {resp.text[:300]}"
            print(f"[supabase] POST {table} failed: {err}")
            return None, err
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0], ""
        return (data if isinstance(data, dict) else None), ""


def rest_patch(table: str, match: Dict[str, str], payload: Dict[str, Any]) -> bool:
    url = f"{env('SUPABASE_URL').rstrip('/')}/rest/v1/{table}"
    params = {k: f"eq.{v}" for k, v in match.items()}
    with httpx.Client(timeout=20) as client:
        resp = client.patch(url, headers=rest_headers("return=minimal"), params=params, json=payload)
        return resp.status_code < 400


def rest_patch_filter(table: str, filters: Dict[str, str], payload: Dict[str, Any]) -> bool:
    """PATCH rows matching PostgREST filter params (e.g. id=in.(uuid1,uuid2))."""
    url = f"{env('SUPABASE_URL').rstrip('/')}/rest/v1/{table}"
    if not env("SUPABASE_URL") or not env("SUPABASE_SERVICE_KEY"):
        return False
    with httpx.Client(timeout=20) as client:
        resp = client.patch(url, headers=rest_headers("return=minimal"), params=filters, json=payload)
        return resp.status_code < 400


def rest_delete(table: str, match: Dict[str, str]) -> tuple[bool, str]:
    url = f"{env('SUPABASE_URL').rstrip('/')}/rest/v1/{table}"
    if not env("SUPABASE_URL") or not env("SUPABASE_SERVICE_KEY"):
        return False, "SUPABASE_URL or SUPABASE_SERVICE_KEY not configured"
    params = {k: f"eq.{v}" for k, v in match.items()}
    with httpx.Client(timeout=20) as client:
        resp = client.delete(url, headers=rest_headers("return=minimal"), params=params)
        if resp.status_code >= 400:
            err = f"HTTP {resp.status_code}: {resp.text[:300]}"
            print(f"[supabase] DELETE {table} failed: {err}")
            return False, err
        return True, ""


def client_id_from_user_id(user_id: str) -> str:
    """Resolve tenant client_id for a user via service-role client_members lookup."""
    uid = (user_id or "").strip()
    if not uid:
        raise ValueError("no client membership for user ")
    rows = rest_get(
        "client_members",
        {
            "user_id": f"eq.{uid}",
            "select": "client_id,created_at",
            "order": "created_at.asc",
        },
    )
    if not rows:
        raise ValueError(f"no client membership for user {uid}")
    # TODO: multi-workspace selector when users belong to multiple clients
    return str(rows[0]["client_id"])


def user_id_from_bearer(token: str) -> Optional[str]:
    if not token:
        return None
    url, anon = env("SUPABASE_URL"), env("SUPABASE_ANON_KEY")
    if not url or not anon:
        return None
    try:
        from supabase import create_client

        client = create_client(url, anon)
        user = client.auth.get_user(token).user
        return str(user.id) if user else None
    except Exception:
        return None


def user_id_from_email(email: str) -> Optional[str]:
    target = (email or "").strip().lower()
    if not target:
        return None
    url = env("SUPABASE_URL").rstrip("/")
    key = env("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return None
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    page = 1
    with httpx.Client(timeout=30) as client:
        while True:
            resp = client.get(
                f"{url}/auth/v1/admin/users",
                headers=headers,
                params={"page": page, "per_page": 200},
            )
            if resp.status_code >= 400:
                return None
            body = resp.json()
            users = body.get("users") if isinstance(body, dict) else []
            for user in users:
                if (user.get("email") or "").strip().lower() == target:
                    return str(user.get("id"))
            if len(users) < 200:
                break
            page += 1
    return None


def pause_workflows_for_user(user_id: str, *, active_only: bool = False) -> Tuple[int, str]:
    if not user_id:
        return 0, "user_id is required"
    url = f"{env('SUPABASE_URL').rstrip('/')}/rest/v1/workflows"
    if not env("SUPABASE_URL") or not env("SUPABASE_SERVICE_KEY"):
        return 0, "SUPABASE_URL or SUPABASE_SERVICE_KEY not configured"
    params: Dict[str, str] = {"user_id": f"eq.{user_id}"}
    if active_only:
        params["status"] = "eq.active"
    payload = {"status": "paused", "updated_at": datetime.now(timezone.utc).isoformat()}
    with httpx.Client(timeout=20) as client:
        resp = client.patch(
            url,
            headers=rest_headers("return=representation"),
            params=params,
            json=payload,
        )
        if resp.status_code >= 400:
            err = f"HTTP {resp.status_code}: {resp.text[:300]}"
            print(f"[supabase] PATCH workflows failed: {err}")
            return 0, err
        data = resp.json()
        if isinstance(data, list):
            return len(data), ""
        return 0, ""
