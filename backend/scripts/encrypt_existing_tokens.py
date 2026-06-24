"""Encrypt existing plaintext token fields in user_integrations. Safe to re-run."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))
load_dotenv(_BACKEND / ".env")

from crypto import encrypt_token_data  # noqa: E402


def _headers() -> dict[str, str]:
    url = (os.environ.get("SUPABASE_URL") or "").strip()
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY are required")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def main() -> None:
    base = os.environ["SUPABASE_URL"].rstrip("/")
    headers = _headers()
    resp = httpx.get(
        f"{base}/rest/v1/user_integrations",
        headers=headers,
        params={"select": "id,token_data"},
        timeout=30.0,
    )
    resp.raise_for_status()
    rows = resp.json()

    encrypted = 0
    already_encrypted = 0
    skipped = 0

    for row in rows:
        token_data = row.get("token_data")
        if not token_data:
            skipped += 1
            continue

        new_token_data = encrypt_token_data(token_data)
        if new_token_data == token_data:
            already_encrypted += 1
            continue

        patch = httpx.patch(
            f"{base}/rest/v1/user_integrations",
            headers=headers,
            params={"id": f"eq.{row['id']}"},
            json={"token_data": new_token_data},
            timeout=30.0,
        )
        patch.raise_for_status()
        encrypted += 1

    print(
        f"Processed {len(rows)} rows: {encrypted} encrypted, "
        f"{already_encrypted} already encrypted, {skipped} skipped (no token_data)"
    )


if __name__ == "__main__":
    main()
