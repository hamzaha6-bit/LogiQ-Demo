"""Encrypt sensitive OAuth token fields at rest in user_integrations.token_data."""

from __future__ import annotations

import copy
import os
from typing import Any, Dict

from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "v1:"
_SENSITIVE_KEYS = frozenset({"token", "refresh_token", "client_secret"})


def _load_fernet() -> Fernet:
    raw = (os.environ.get("TOKEN_ENCRYPTION_KEY") or "").strip()
    if not raw:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY is not set — required for OAuth token encryption at rest"
        )
    try:
        return Fernet(raw.encode("ascii"))
    except Exception as exc:
        raise RuntimeError(f"TOKEN_ENCRYPTION_KEY is invalid for Fernet: {exc}") from exc


_FERNET = _load_fernet()


def encrypt_field(plaintext: str) -> str:
    if not plaintext:
        return plaintext
    if plaintext.startswith(_PREFIX):
        return plaintext
    ciphertext = _FERNET.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{ciphertext}"


def decrypt_field(value: str) -> str:
    if not value:
        return value
    if not value.startswith(_PREFIX):
        return value
    ciphertext = value[len(_PREFIX) :]
    try:
        return _FERNET.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError(
            "Failed to decrypt token field — ciphertext tampered or TOKEN_ENCRYPTION_KEY mismatch"
        ) from exc


def encrypt_token_data(token_data: Dict[str, Any]) -> Dict[str, Any]:
    if not token_data:
        return token_data
    out = copy.deepcopy(token_data)
    for key in _SENSITIVE_KEYS:
        val = out.get(key)
        if isinstance(val, str) and val:
            out[key] = encrypt_field(val)
    return out


def decrypt_token_data(token_data: Dict[str, Any]) -> Dict[str, Any]:
    if not token_data:
        return token_data
    out = copy.deepcopy(token_data)
    for key in _SENSITIVE_KEYS:
        val = out.get(key)
        if isinstance(val, str) and val:
            out[key] = decrypt_field(val)
    return out


# ============================================================================
# FOLLOW-UP (out of scope for user_integrations.token_data encryption):
#   • backend/main.py oauth_tokens scaffold table (access_token / refresh_token)
#   • GMAIL_TOKEN_JSON, GMAIL_CREDENTIALS_JSON, GOOGLE_SHEETS_CREDENTIALS_JSON env vars
#   • backend/token.json local dev fallback (backend/gmail_service.py)
#   • Xero / HubSpot env-stored credentials
# ============================================================================
