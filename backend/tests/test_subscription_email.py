"""Tests for subscription confirmation email (steps 9+10)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
_API_LIB = _ROOT / "api_lib"
sys.path.insert(0, str(_API_LIB))

os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("GMAIL_SENDER_EMAIL", "sender@test.example")
os.environ.setdefault("GMAIL_TOKEN_JSON", '{"token": "test"}')
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

from hook_handler import send_subscription_confirmation  # noqa: E402
from supabase_rest import email_from_user_id  # noqa: E402

CLIENT_ID = "11111111-2222-4333-8444-555555555555"
USER_A = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
USER_B = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


@patch("supabase_rest.httpx.Client")
def test_email_from_user_id_returns_email(mock_client_cls: MagicMock) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"email": "alice@example.com"}
    mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp

    result = email_from_user_id(USER_A)

    assert result == "alice@example.com"
    call_url = mock_client_cls.return_value.__enter__.return_value.get.call_args.args[0]
    assert USER_A in call_url


@patch("supabase_rest.httpx.Client")
def test_email_from_user_id_returns_none_on_404(mock_client_cls: MagicMock) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp

    assert email_from_user_id(USER_A) is None


@patch("hook_handler.send_platform_email", return_value=(True, "msg_123"))
@patch("hook_handler.email_from_user_id", side_effect=["alice@example.com", "bob@example.com"])
@patch("hook_handler.member_user_ids", return_value=[USER_A, USER_B])
@patch("hook_handler.is_gmail_configured", return_value=True)
def test_send_subscription_confirmation_sends_to_all_members(
    mock_gmail: MagicMock,
    mock_members: MagicMock,
    mock_email_lookup: MagicMock,
    mock_send: MagicMock,
) -> None:
    send_subscription_confirmation(CLIENT_ID, "starter")

    assert mock_send.call_count == 2
    recipients = {call.kwargs["to"] for call in mock_send.call_args_list}
    assert recipients == {"alice@example.com", "bob@example.com"}


@patch("hook_handler.send_platform_email", return_value=(True, "msg_123"))
@patch("hook_handler.email_from_user_id", side_effect=["alice@example.com", None])
@patch("hook_handler.member_user_ids", return_value=[USER_A, USER_B])
@patch("hook_handler.is_gmail_configured", return_value=True)
@patch("hook_handler.rest_post")
def test_send_subscription_confirmation_skips_failed_email_lookup(
    mock_audit: MagicMock,
    mock_gmail: MagicMock,
    mock_members: MagicMock,
    mock_email_lookup: MagicMock,
    mock_send: MagicMock,
) -> None:
    failures = send_subscription_confirmation(CLIENT_ID, "pro")

    assert mock_send.call_count == 1
    assert mock_send.call_args.kwargs["to"] == "alice@example.com"
    assert failures  # missing email for USER_B is a recorded failure
    mock_audit.assert_called()


@patch("hook_handler.send_platform_email", return_value=(True, "msg_123"))
@patch("hook_handler.email_from_user_id", return_value="hamza.arif@example.com")
@patch("hook_handler.member_user_ids", return_value=[USER_A])
@patch("hook_handler.is_gmail_configured", return_value=True)
def test_send_subscription_confirmation_uses_tier_name_and_action_count(
    mock_gmail: MagicMock,
    mock_members: MagicMock,
    mock_email_lookup: MagicMock,
    mock_send: MagicMock,
) -> None:
    send_subscription_confirmation(CLIENT_ID, "starter")

    body = mock_send.call_args.kwargs["body"]
    assert "Your Starter subscription is now active" in body
    assert "you're live on LogiQ" in body
    assert "LogiQ will build it for you" in body
    assert "Your plan: Starter — 500 actions per month." in body
    assert mock_send.call_args.kwargs["subject"] == (
        "Your LogiQ subscription is active - here's what to do next"
    )


@patch("hook_handler.send_platform_email", return_value=(False, "smtp boom"))
@patch("hook_handler.email_from_user_id", return_value="alice@example.com")
@patch("hook_handler.member_user_ids", return_value=[USER_A])
@patch("hook_handler.is_gmail_configured", return_value=True)
@patch("hook_handler.rest_post")
def test_send_subscription_confirmation_records_failure(
    mock_audit: MagicMock,
    mock_gmail: MagicMock,
    mock_members: MagicMock,
    mock_email_lookup: MagicMock,
    mock_send: MagicMock,
) -> None:
    failures = send_subscription_confirmation(CLIENT_ID, "starter")
    assert failures
    assert any("alice@example.com" in f for f in failures)
    mock_audit.assert_called()
    assert mock_audit.call_args[0][0] == "audit_log"
    entry = mock_audit.call_args[0][1]
    assert entry["action_type"] == "subscription_email_failed"
    assert entry["status"] == "failed"


@patch("hook_handler.send_platform_email", side_effect=RuntimeError("network down"))
@patch("hook_handler.email_from_user_id", return_value="alice@example.com")
@patch("hook_handler.member_user_ids", return_value=[USER_A])
@patch("hook_handler.is_gmail_configured", return_value=True)
@patch("hook_handler.rest_post")
def test_send_subscription_confirmation_exception_does_not_raise(
    mock_audit: MagicMock,
    mock_gmail: MagicMock,
    mock_members: MagicMock,
    mock_email_lookup: MagicMock,
    mock_send: MagicMock,
) -> None:
    failures = send_subscription_confirmation(CLIENT_ID, "starter")
    assert failures
    assert any("network down" in f for f in failures)
    mock_audit.assert_called()

