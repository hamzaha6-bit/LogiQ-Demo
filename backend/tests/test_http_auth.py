"""G4 — never accept a session token from the query string."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from http_auth import resolve_access_token  # noqa: E402


def test_query_token_is_ignored():
    handler = SimpleNamespace(
        path="/api/auth/gmail/connect?token=stolen-jwt",
        headers={},
    )
    assert resolve_access_token(handler) is None


def test_authorization_header_is_used():
    handler = SimpleNamespace(
        path="/api/auth/gmail/connect?token=stolen-jwt",
        headers={"Authorization": "Bearer real-session"},
    )
    assert resolve_access_token(handler) == "real-session"
