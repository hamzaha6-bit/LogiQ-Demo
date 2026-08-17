"""G1 — Google OAuth state must be HMAC-signed and time-limited."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from cryptography.fernet import Fernet

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from gmail_oauth import _decode_oauth_state, _encode_oauth_state  # noqa: E402

USER_ID = "cccccccccccccccc-4ddd-8eee-ffff-111111111111"


def test_signed_state_roundtrip():
    state = _encode_oauth_state(USER_ID)
    uid, token = _decode_oauth_state(state)
    assert uid == USER_ID
    assert token is None
    assert "." in state


def test_unsigned_legacy_state_rejected():
    uid, token = _decode_oauth_state("eyJ1c2VyX2lkIjoiZXZpbCJ9")
    assert uid is None
    assert token is None


def test_tampered_hmac_rejected():
    state = _encode_oauth_state(USER_ID)
    body, sig = state.rsplit(".", 1)
    uid, _ = _decode_oauth_state(f"{body}.{sig[:-1]}x")
    assert uid is None


def test_expired_state_rejected(monkeypatch):
    state = _encode_oauth_state(USER_ID)
    monkeypatch.setattr("gmail_oauth.time.time", lambda: time.time() + 16 * 60)
    uid, _ = _decode_oauth_state(state)
    assert uid is None
