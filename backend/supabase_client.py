"""Supabase client helpers."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("logiq.supabase")

_client = None


def is_configured() -> bool:
    return bool(
        (os.getenv("SUPABASE_URL") or "").strip()
        and (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()
    )


def get_anon_key() -> str:
    return (os.getenv("SUPABASE_ANON_KEY") or "").strip()


def get_url() -> str:
    return (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")


def get_client():
    global _client
    if not is_configured():
        return None
    if _client is None:
        from supabase import create_client

        _client = create_client(get_url(), os.getenv("SUPABASE_SERVICE_KEY", "").strip())
    return _client


def rest_headers() -> Dict[str, str]:
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def rest_get(table: str, params: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    import httpx

    url = f"{get_url()}/rest/v1/{table}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, headers=rest_headers(), params=params or {})
        if resp.status_code >= 400:
            logger.warning("Supabase GET %s failed: %s", table, resp.text[:200])
            return []
        data = resp.json()
        return data if isinstance(data, list) else []


async def rest_post(table: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    import httpx

    url = f"{get_url()}/rest/v1/{table}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, headers=rest_headers(), json=payload)
        if resp.status_code >= 400:
            logger.warning("Supabase POST %s failed: %s", table, resp.text[:200])
            return None
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        return data if isinstance(data, dict) else None


async def rest_patch(table: str, match: Dict[str, str], payload: Dict[str, Any]) -> bool:
    import httpx

    url = f"{get_url()}/rest/v1/{table}"
    params = {f"{k}": f"eq.{v}" for k, v in match.items()}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.patch(url, headers=rest_headers(), params=params, json=payload)
        if resp.status_code >= 400:
            logger.warning("Supabase PATCH %s failed: %s", table, resp.text[:200])
            return False
        return True


async def rest_upsert(table: str, payload: Dict[str, Any], on_conflict: str = "") -> bool:
    import httpx

    url = f"{get_url()}/rest/v1/{table}"
    headers = {**rest_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
    params = {"on_conflict": on_conflict} if on_conflict else None
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, headers=headers, params=params, json=payload)
        if resp.status_code >= 400:
            logger.warning("Supabase UPSERT %s failed: %s", table, resp.text[:200])
            return False
        return True
