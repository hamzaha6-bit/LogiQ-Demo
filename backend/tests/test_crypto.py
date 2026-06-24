"""Tests for per-field OAuth token encryption."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from dotenv import load_dotenv

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))
load_dotenv(_BACKEND / ".env")

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

import crypto  # noqa: E402

from crypto import (  # noqa: E402
    decrypt_field,
    decrypt_token_data,
    encrypt_field,
    encrypt_token_data,
)

SAMPLE_TOKEN_DATA = {
    "token": "ya29.access-token-value",
    "refresh_token": "1//refresh-token-value",
    "client_secret": "GOCSPX-secret",
    "client_id": "123.apps.googleusercontent.com",
    "token_uri": "https://oauth2.googleapis.com/token",
    "scopes": [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
    ],
    "expiry": "2026-06-15T12:00:00Z",
    "connected_at": "2026-06-01T10:00:00Z",
}


def test_encrypt_decrypt_field_round_trip() -> None:
    plaintext = "super-secret-access-token"
    encrypted = encrypt_field(plaintext)
    assert encrypted.startswith("v1:")
    assert encrypted != plaintext
    assert decrypt_field(encrypted) == plaintext


def test_decrypt_field_legacy_passthrough() -> None:
    legacy = "plain-refresh-token-without-prefix"
    assert decrypt_field(legacy) == legacy


def test_encrypt_token_data_idempotent() -> None:
    once = encrypt_token_data(SAMPLE_TOKEN_DATA)
    twice = encrypt_token_data(once)
    assert twice == once


def test_encrypt_decrypt_token_data_round_trip() -> None:
    encrypted = encrypt_token_data(SAMPLE_TOKEN_DATA)
    assert encrypted["client_id"] == SAMPLE_TOKEN_DATA["client_id"]
    assert encrypted["token_uri"] == SAMPLE_TOKEN_DATA["token_uri"]
    assert encrypted["scopes"] == SAMPLE_TOKEN_DATA["scopes"]
    assert encrypted["expiry"] == SAMPLE_TOKEN_DATA["expiry"]
    assert encrypted["connected_at"] == SAMPLE_TOKEN_DATA["connected_at"]
    assert encrypted["token"].startswith("v1:")
    assert encrypted["refresh_token"].startswith("v1:")
    assert encrypted["client_secret"].startswith("v1:")

    decrypted = decrypt_token_data(encrypted)
    assert decrypted == SAMPLE_TOKEN_DATA


def test_decrypt_field_tampered_ciphertext_raises() -> None:
    encrypted = encrypt_field("sensitive-value")
    tampered = encrypted[:-1] + ("a" if encrypted[-1] != "a" else "b")
    with pytest.raises(ValueError, match="tampered|mismatch"):
        decrypt_field(tampered)


def test_decrypt_field_wrong_key_raises() -> None:
    original_key = os.environ["TOKEN_ENCRYPTION_KEY"]
    encrypted = encrypt_field("sensitive-value")
    os.environ["TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    importlib.reload(crypto)
    try:
        with pytest.raises(ValueError, match="tampered|mismatch"):
            crypto.decrypt_field(encrypted)
    finally:
        os.environ["TOKEN_ENCRYPTION_KEY"] = original_key
        importlib.reload(crypto)
