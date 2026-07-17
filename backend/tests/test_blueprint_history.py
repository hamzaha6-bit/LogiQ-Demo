"""Tests for Blueprint history helpers and free-preview counting."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_dummy")

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from blueprint_history import (  # noqa: E402
    FREE_PREVIEW_USER_MESSAGE_LIMIT,
    VALID_AGENTS,
    cap_messages_for_claude,
    count_user_blueprint_messages,
    normalize_agent_id,
)
from execution_gate import check_blueprint_chat_gate  # noqa: E402

USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CLIENT_ID = "11111111-2222-4333-8444-555555555555"

ACTIVE_ENTITLEMENT = {
    "client_id": CLIENT_ID,
    "status": "active",
    "plan": "starter",
    "actions_limit": 500,
    "agents_limit": 2,
    "spend_cap_pence": 4000,
}


def test_all_five_agents_are_valid():
    assert VALID_AGENTS == frozenset({"aria", "nova", "finn", "zara", "cleo"})
    for aid in VALID_AGENTS:
        assert normalize_agent_id(aid) == aid
    assert normalize_agent_id("ARIA") == "aria"
    assert normalize_agent_id("unknown") is None


def test_cap_messages_keeps_last_20():
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(30)]
    capped = cap_messages_for_claude(msgs, 20)
    assert len(capped) == 20
    assert capped[0]["content"] == "10"
    assert capped[-1]["content"] == "29"


@patch("blueprint_history.rest_get")
def test_count_user_messages_sums_across_all_agents(mock_get: MagicMock) -> None:
    # Five user messages across different agents — total is what free preview uses.
    mock_get.return_value = [
        {"id": "1"},
        {"id": "2"},
        {"id": "3"},
        {"id": "4"},
        {"id": "5"},
    ]
    assert count_user_blueprint_messages(USER_ID) == 5
    params = mock_get.call_args[0][1]
    assert params["user_id"] == f"eq.{USER_ID}"
    assert params["role"] == "eq.user"
    assert "agent_id" not in params


@patch("execution_gate.count_user_blueprint_messages", return_value=4)
@patch("execution_gate.get_entitlement", return_value=None)
@patch("execution_gate.client_id_from_user_id", return_value=CLIENT_ID)
def test_free_preview_allows_fifth_message(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
    mock_count: MagicMock,
) -> None:
    gate = check_blueprint_chat_gate(USER_ID)
    assert gate.allowed is True
    assert gate.free_preview is True
    assert gate.client_id == CLIENT_ID


@patch("execution_gate.count_user_blueprint_messages", return_value=FREE_PREVIEW_USER_MESSAGE_LIMIT)
@patch("execution_gate.get_entitlement", return_value={"status": "canceled"})
@patch("execution_gate.client_id_from_user_id", return_value=CLIENT_ID)
def test_free_preview_blocks_sixth_message_total(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
    mock_count: MagicMock,
) -> None:
    gate = check_blueprint_chat_gate(USER_ID)
    assert gate.allowed is False
    assert gate.error == "free_preview_exhausted"
    assert "free preview" in gate.reason.lower()
    mock_count.assert_called_once_with(USER_ID)


@patch("execution_gate.count_active_agents", return_value=0)
@patch("execution_gate.get_monthly_usage", return_value={"actions_used": 0, "spend_pence": 0})
@patch("execution_gate.get_entitlement", return_value=ACTIVE_ENTITLEMENT)
@patch("execution_gate.client_id_from_user_id", return_value=CLIENT_ID)
def test_paying_user_uses_normal_gate_not_preview_count(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
    mock_usage: MagicMock,
    mock_agents: MagicMock,
) -> None:
    with patch("execution_gate.count_user_blueprint_messages") as mock_count:
        gate = check_blueprint_chat_gate(USER_ID)
        assert gate.allowed is True
        assert gate.free_preview is False
        mock_count.assert_not_called()


@patch("execution_gate.email_from_user_id", return_value="owner@logiq.org.uk")
@patch.dict(os.environ, {"OWNER_EMAILS": "owner@logiq.org.uk"}, clear=False)
def test_owner_bypasses_free_preview(mock_email: MagicMock) -> None:
    with patch("execution_gate.count_user_blueprint_messages") as mock_count:
        gate = check_blueprint_chat_gate(USER_ID)
        assert gate.allowed is True
        assert gate.client_id == "owner-bypass"
        mock_count.assert_not_called()
