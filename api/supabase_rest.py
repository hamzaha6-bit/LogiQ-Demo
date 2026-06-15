"""Shared Supabase REST helpers for Vercel API functions."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

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
    url = f"{env('SUPABASE_URL').rstrip('/')}/rest/v1/{table}"
    params = {"on_conflict": on_conflict} if on_conflict else None
    with httpx.Client(timeout=20) as client:
        resp = client.post(url, headers=rest_headers(prefer), params=params, json=payload)
        if resp.status_code >= 400:
            print(f"[supabase] POST {table} failed: {resp.status_code} {resp.text[:300]}")
            return None
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        return data if isinstance(data, dict) else None


def rest_patch(table: str, match: Dict[str, str], payload: Dict[str, Any]) -> bool:
    url = f"{env('SUPABASE_URL').rstrip('/')}/rest/v1/{table}"
    params = {k: f"eq.{v}" for k, v in match.items()}
    with httpx.Client(timeout=20) as client:
        resp = client.patch(url, headers=rest_headers("return=minimal"), params=params, json=payload)
        return resp.status_code < 400


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
