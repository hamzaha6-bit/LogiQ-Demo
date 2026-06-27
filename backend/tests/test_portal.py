"""Tests for Stripe Customer Portal (step 8)."""

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

from billing_portal import PortalError, process_portal  # noqa: E402

USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CLIENT_ID = "11111111-2222-4333-8444-555555555555"

ACTIVE_ENTITLEMENT = {
    "client_id": CLIENT_ID,
    "status": "active",
    "stripe_customer_id": "cus_test_active",
}


@patch("billing_portal.get_stripe")
@patch("billing_portal.get_entitlement", return_value=ACTIVE_ENTITLEMENT)
@patch("billing_portal.client_id_from_user_id", return_value=CLIENT_ID)
def test_active_subscriber_returns_portal_url(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
    mock_get_stripe: MagicMock,
) -> None:
    mock_session = MagicMock()
    mock_session.url = "https://billing.stripe.com/p/session/test_portal"
    mock_get_stripe.return_value.billing_portal.Session.create.return_value = mock_session

    result = process_portal(USER_ID)

    assert result["url"].startswith("https://billing.stripe.com/")
    create_kwargs = mock_get_stripe.return_value.billing_portal.Session.create.call_args.kwargs
    assert create_kwargs["customer"] == "cus_test_active"
    assert create_kwargs["return_url"] == "https://app.logiqops.co.uk/billing/success"


@patch("billing_portal.get_entitlement", return_value={**ACTIVE_ENTITLEMENT, "stripe_customer_id": None})
@patch("billing_portal.client_id_from_user_id", return_value=CLIENT_ID)
def test_inactive_subscriber_no_customer_id_returns_403(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
) -> None:
    with pytest.raises(PortalError) as exc:
        process_portal(USER_ID)
    assert exc.value.status == 403
    assert exc.value.payload == {
        "error": "no_active_subscription",
        "message": "Please subscribe first.",
    }


@patch("billing_portal.get_entitlement")
@patch("billing_portal.client_id_from_user_id", side_effect=ValueError("no client membership"))
def test_no_client_membership_returns_400(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
) -> None:
    with pytest.raises(PortalError) as exc:
        process_portal(USER_ID)
    assert exc.value.status == 400
    mock_entitlement.assert_not_called()


def test_unauthenticated_returns_401() -> None:
    with pytest.raises(PortalError) as exc:
        process_portal(None)
    assert exc.value.status == 401
