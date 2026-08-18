"""G1 — Google OAuth state must be HMAC-signed, time-limited, and CSRF-bound."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from cryptography.fernet import Fernet

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from gmail_oauth import (  # noqa: E402
    OAUTH_NONCE_COOKIE,
    _decode_oauth_state,
    _encode_oauth_state,
    _oauth_nonce_cookie_header,
    _oauth_nonce_matches,
)

USER_ID = "cccccccccccccccc-4ddd-8eee-ffff-111111111111"


def test_signed_state_roundtrip():
    state, nonce = _encode_oauth_state(USER_ID)
    uid, got_nonce = _decode_oauth_state(state)
    assert uid == USER_ID
    assert got_nonce == nonce
    assert "." in state


def test_unsigned_legacy_state_rejected():
    uid, nonce = _decode_oauth_state("eyJ1c2VyX2lkIjoiZXZpbCJ9")
    assert uid is None
    assert nonce is None


def test_tampered_hmac_rejected():
    state, _nonce = _encode_oauth_state(USER_ID)
    body, sig = state.rsplit(".", 1)
    uid, _ = _decode_oauth_state(f"{body}.{sig[:-1]}x")
    assert uid is None


def test_expired_state_rejected(monkeypatch):
    state, _nonce = _encode_oauth_state(USER_ID)
    monkeypatch.setattr("gmail_oauth.time.time", lambda: time.time() + 16 * 60)
    uid, _ = _decode_oauth_state(state)
    assert uid is None


def test_oauth_nonce_cookie_matches_state():
    _state, nonce = _encode_oauth_state(USER_ID)
    cookie = f"{OAUTH_NONCE_COOKIE}={nonce}"
    assert _oauth_nonce_matches(nonce, cookie)
    assert not _oauth_nonce_matches(nonce, "")
    assert not _oauth_nonce_matches(nonce, None)
    assert not _oauth_nonce_matches(nonce, f"{OAUTH_NONCE_COOKIE}=other-nonce")


def test_oauth_nonce_missing_cookie_fails_closed():
    _state, nonce = _encode_oauth_state(USER_ID)
    assert not _oauth_nonce_matches(nonce, "session=abc")
    assert not _oauth_nonce_matches("", f"{OAUTH_NONCE_COOKIE}=anything")


def test_oauth_nonce_cookie_header_flags():
    header = _oauth_nonce_cookie_header("abc123")
    assert header.startswith(f"{OAUTH_NONCE_COOKIE}=abc123")
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=Lax" in header
    assert "Path=/api/auth/gmail" in header
    clear = _oauth_nonce_cookie_header("", clear=True)
    assert "Max-Age=0" in clear
