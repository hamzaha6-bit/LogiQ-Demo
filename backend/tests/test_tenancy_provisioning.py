"""G11 — provision clients/client_members/entitlements for a new user."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from supabase_rest import ensure_client_membership  # noqa: E402

USER_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"


@patch("supabase_rest.rest_post")
@patch("supabase_rest.rest_post_with_error")
@patch("supabase_rest.rest_get")
def test_ensure_creates_client_member_and_entitlement(mock_get, mock_post_err, mock_post):
    mock_get.return_value = []
    mock_post_err.side_effect = [
        ({"id": "client-1"}, ""),
        ({"client_id": "client-1", "user_id": USER_ID}, ""),
    ]
    mock_post.return_value = {"client_id": "client-1"}
    cid = ensure_client_membership(USER_ID, display_name="Pound Fabrics")
    assert cid == "client-1"
    tables = [c.args[0] for c in mock_post_err.call_args_list]
    assert tables[0] == "clients"
    assert mock_post_err.call_args_list[0].args[1]["name"] == "Pound Fabrics"
    assert tables[1] == "client_members"
    assert mock_post_err.call_args_list[1].args[1]["role"] == "owner"
    mock_post.assert_called_once()
    assert mock_post.call_args.args[0] == "entitlements"


@patch("supabase_rest.rest_post_with_error")
@patch("supabase_rest.rest_get")
def test_ensure_is_idempotent_when_membership_exists(mock_get, mock_post_err):
    mock_get.return_value = [{"client_id": "existing", "created_at": "2026-01-01"}]
    cid = ensure_client_membership(USER_ID)
    assert cid == "existing"
    mock_post_err.assert_not_called()
