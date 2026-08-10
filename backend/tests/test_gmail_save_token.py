"""save_user_token must include client_id (NOT NULL on user_integrations)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

import google_oauth  # noqa: E402


CLIENT_ID = "a1111111-1111-4111-8111-111111111111"
USER_ID = "u2222222-2222-4222-8222-222222222222"


@patch("google_oauth.rest_post_with_error", return_value=({"id": "row-1"}, ""))
@patch("google_oauth.client_id_from_user_id", return_value=CLIENT_ID)
@patch("google_oauth.encrypt_token_data", side_effect=lambda d: {"token": "enc"})
def test_save_user_token_includes_client_id(mock_enc, mock_client, mock_post):
    ok, err = google_oauth.save_user_token(USER_ID, {"token": "plain", "refresh_token": "r"})
    assert ok is True
    assert err == ""
    mock_client.assert_called_once_with(USER_ID)
    assert mock_post.call_args[0][0] == "user_integrations"
    payload = mock_post.call_args[0][1]
    assert payload["user_id"] == USER_ID
    assert payload["client_id"] == CLIENT_ID
    assert payload["integration"] == "gmail"
    assert payload["token_data"] == {"token": "enc"}
    assert "connected_at" in payload


@patch("google_oauth.rest_post_with_error")
@patch(
    "google_oauth.client_id_from_user_id",
    side_effect=ValueError("no client membership for user u1"),
)
def test_save_user_token_fails_without_client_membership(mock_client, mock_post):
    ok, err = google_oauth.save_user_token("u1", {"token": "plain"})
    assert ok is False
    assert "no client membership" in err
    mock_post.assert_not_called()
