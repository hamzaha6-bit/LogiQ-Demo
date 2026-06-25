"""Tests for the execution gate and client-scoped usage tracking."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_dummy")

_ROOT = Path(__file__).resolve().parent.parent.parent
_API_LIB = _ROOT / "api_lib"
sys.path.insert(0, str(_API_LIB))

from execution_gate import (  # noqa: E402
    check_execution_gate,
    record_allowed_action,
)

USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CLIENT_ID = "11111111-2222-4333-8444-555555555555"

ACTIVE_ENTITLEMENT = {
    "client_id": CLIENT_ID,
    "status": "active",
    "plan": "starter",
    "actions_limit": 500,
    "spend_cap_pence": 4000,
}


@patch("execution_gate.get_monthly_usage", return_value={"actions_used": 0, "spend_pence": 0})
@patch("execution_gate.get_entitlement", return_value=ACTIVE_ENTITLEMENT)
@patch("execution_gate.client_id_from_user_id", return_value=CLIENT_ID)
def test_active_subscription_allows_action(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
    mock_usage: MagicMock,
) -> None:
    result = check_execution_gate(USER_ID, "agent_action")
    assert result.allowed is True
    assert result.client_id == CLIENT_ID


@patch("execution_gate.get_monthly_usage", return_value={"actions_used": 0, "spend_pence": 0})
@patch("execution_gate.get_entitlement", return_value={**ACTIVE_ENTITLEMENT, "status": "inactive"})
@patch("execution_gate.client_id_from_user_id", return_value=CLIENT_ID)
def test_inactive_subscription_blocks(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
    mock_usage: MagicMock,
) -> None:
    result = check_execution_gate(USER_ID, "agent_action")
    assert result.allowed is False
    assert result.error == "no_active_subscription"


@patch("execution_gate.get_monthly_usage", return_value={"actions_used": 500, "spend_pence": 0})
@patch("execution_gate.get_entitlement", return_value=ACTIVE_ENTITLEMENT)
@patch("execution_gate.client_id_from_user_id", return_value=CLIENT_ID)
def test_over_action_limit_blocks(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
    mock_usage: MagicMock,
) -> None:
    result = check_execution_gate(USER_ID, "agent_action")
    assert result.allowed is False
    assert result.error == "action_limit_reached"


@patch("execution_gate.get_monthly_usage", return_value={"actions_used": 0, "spend_pence": 3995})
@patch("execution_gate.get_entitlement", return_value=ACTIVE_ENTITLEMENT)
@patch("execution_gate.client_id_from_user_id", return_value=CLIENT_ID)
def test_over_spend_cap_blocks(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
    mock_usage: MagicMock,
) -> None:
    result = check_execution_gate(USER_ID, "agent_action")
    assert result.allowed is False
    assert result.error == "spend_cap_reached"


@patch("execution_gate._record_client_action")
@patch("execution_gate.get_monthly_usage", return_value={"actions_used": 1, "spend_pence": 10})
@patch("execution_gate.get_entitlement", return_value=ACTIVE_ENTITLEMENT)
@patch("execution_gate.client_id_from_user_id", return_value=CLIENT_ID)
def test_usage_recorded_after_allowed_action(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
    mock_usage: MagicMock,
    mock_record: MagicMock,
) -> None:
    gate = check_execution_gate(USER_ID, "agent_action")
    assert gate.allowed is True
    record_allowed_action(gate.client_id, "agent_action")
    mock_record.assert_called_once_with(CLIENT_ID, 10)


@patch("execution_gate._record_client_action")
@patch("execution_gate.get_monthly_usage", return_value={"actions_used": 500, "spend_pence": 0})
@patch("execution_gate.get_entitlement", return_value=ACTIVE_ENTITLEMENT)
@patch("execution_gate.client_id_from_user_id", return_value=CLIENT_ID)
def test_usage_not_recorded_when_blocked(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
    mock_usage: MagicMock,
    mock_record: MagicMock,
) -> None:
    gate = check_execution_gate(USER_ID, "agent_action")
    assert gate.allowed is False
    if gate.allowed:
        record_allowed_action(gate.client_id, "agent_action")
    mock_record.assert_not_called()
