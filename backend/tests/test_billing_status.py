"""Tests for /api/billing/status (step 6 — entitlements + client_usage)."""

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

from billing_status import (  # noqa: E402
    billing_status_for_request,
    get_billing_status,
)

USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CLIENT_ID = "11111111-2222-4333-8444-555555555555"

ACTIVE_ENTITLEMENT = {
    "client_id": CLIENT_ID,
    "status": "active",
    "plan": "starter",
    "actions_limit": 500,
    "agents_limit": 1,
    "workflows_limit": 2,
    "spend_cap_pence": 4000,
}

INACTIVE_SHAPE_KEYS = {
    "plan",
    "plan_name",
    "status",
    "usage",
    "limits",
    "percentages",
    "spend",
    "stripe_configured",
}


@patch("billing_status.get_monthly_usage", return_value={"actions_used": 240, "spend_pence": 120})
@patch("billing_status.get_entitlement", return_value=ACTIVE_ENTITLEMENT)
@patch("billing_status.client_id_from_user_id", return_value=CLIENT_ID)
def test_active_subscriber_returns_usage_and_limits(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
    mock_usage: MagicMock,
) -> None:
    result = get_billing_status(USER_ID)

    assert result["plan"] == "starter"
    assert result["plan_name"] == "Starter"
    assert result["status"] == "active"
    assert result["usage"]["actions_this_month"] == 240
    assert result["usage"]["api_calls_today"] == 0
    assert result["usage"]["emails_sent_today"] == 0
    assert result["limits"]["max_actions_month"] == 500
    assert result["limits"]["max_agents"] == 1
    assert result["limits"]["max_workflows"] == 2
    assert result["limits"]["max_api_calls_day"] == 0
    assert result["limits"]["max_emails_day"] == 0
    assert result["spend"]["used_pence"] == 120
    assert result["spend"]["cap_pence"] == 4000
    assert result["stripe_configured"] is True


@patch("billing_status.get_monthly_usage")
@patch("billing_status.get_entitlement", return_value={**ACTIVE_ENTITLEMENT, "status": "inactive"})
@patch("billing_status.client_id_from_user_id", return_value=CLIENT_ID)
def test_inactive_subscriber_returns_inactive_shape(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
    mock_usage: MagicMock,
) -> None:
    result = get_billing_status(USER_ID)

    assert result["plan"] == "inactive"
    assert result["status"] == "inactive"
    assert result["usage"]["actions_this_month"] == 0
    assert result["limits"]["max_actions_month"] == 0
    assert result["percentages"]["actions"] == 0
    assert result["spend"]["used_pence"] == 0
    mock_usage.assert_not_called()


@patch("billing_status.get_monthly_usage")
@patch("billing_status.get_entitlement")
@patch("billing_status.client_id_from_user_id", side_effect=ValueError("no client membership"))
def test_no_client_membership_returns_inactive_shape(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
    mock_usage: MagicMock,
) -> None:
    result = get_billing_status(USER_ID)

    assert result["plan"] == "inactive"
    assert result["status"] == "inactive"
    assert set(result.keys()) == INACTIVE_SHAPE_KEYS
    mock_entitlement.assert_not_called()
    mock_usage.assert_not_called()


def test_unauthenticated_returns_401() -> None:
    status, payload = billing_status_for_request(None)
    assert status == 401
    assert payload == {"detail": "Valid Bearer token required"}


@patch("billing_status.get_monthly_usage", return_value={"actions_used": 250, "spend_pence": 0})
@patch("billing_status.get_entitlement", return_value=ACTIVE_ENTITLEMENT)
@patch("billing_status.client_id_from_user_id", return_value=CLIENT_ID)
def test_percentages_computed_correctly(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
    mock_usage: MagicMock,
) -> None:
    result = get_billing_status(USER_ID)
    assert result["percentages"]["actions"] == 50


@patch("billing_status.get_monthly_usage", return_value={"actions_used": 0, "spend_pence": 0})
@patch("billing_status.client_id_from_user_id", return_value=CLIENT_ID)
@pytest.mark.parametrize(
    ("tier", "expected_name"),
    [
        ("spark", "Spark"),
        ("starter", "Starter"),
        ("pro", "Pro"),
        ("business", "Business"),
    ],
)
def test_plan_name_from_entitlement_for_each_tier(
    mock_client_id: MagicMock,
    mock_usage: MagicMock,
    tier: str,
    expected_name: str,
) -> None:
    with patch(
        "billing_status.get_entitlement",
        return_value={**ACTIVE_ENTITLEMENT, "plan": tier},
    ):
        result = get_billing_status(USER_ID)
    assert result["plan"] == tier
    assert result["plan_name"] == expected_name
