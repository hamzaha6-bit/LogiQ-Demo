"""Tests for one-off action top-up checkout and webhook handling."""

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
os.environ.setdefault("STRIPE_TOPUP_PRICE_100", "price_test_topup_100")
os.environ.setdefault("STRIPE_TOPUP_PRICE_500", "price_test_topup_500")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_dummy_for_unit_tests")

from billing_webhook import handle_checkout_session_completed  # noqa: E402
from entitlements import apply_topup  # noqa: E402
from topup_checkout import TopupError, process_topup  # noqa: E402

USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CLIENT_ID = "11111111-2222-4333-8444-555555555555"

ACTIVE_ENTITLEMENT = {
    "client_id": CLIENT_ID,
    "status": "active",
    "plan": "starter",
    "actions_limit": 500,
}


@patch("topup_checkout.get_stripe")
@patch("topup_checkout.get_entitlement", return_value=ACTIVE_ENTITLEMENT)
@patch("topup_checkout.client_id_from_user_id", return_value=CLIENT_ID)
def test_valid_pack_active_subscriber_returns_url(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
    mock_get_stripe: MagicMock,
) -> None:
    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/c/pay/cs_test_topup"
    mock_get_stripe.return_value.checkout.Session.create.return_value = mock_session

    result = process_topup(USER_ID, "100")

    assert result["url"].startswith("https://checkout.stripe.com/")
    create_kwargs = mock_get_stripe.return_value.checkout.Session.create.call_args.kwargs
    assert create_kwargs["mode"] == "payment"
    assert create_kwargs["client_reference_id"] == CLIENT_ID
    assert create_kwargs["metadata"]["topup_actions"] == "100"
    assert create_kwargs["metadata"]["pack_size"] == "100"


@patch("topup_checkout.get_entitlement", return_value=ACTIVE_ENTITLEMENT)
@patch("topup_checkout.client_id_from_user_id", return_value=CLIENT_ID)
def test_invalid_pack_size_returns_400(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
) -> None:
    with pytest.raises(TopupError) as exc:
        process_topup(USER_ID, "999")
    assert exc.value.status == 400


@patch("topup_checkout.get_entitlement", return_value={**ACTIVE_ENTITLEMENT, "status": "inactive"})
@patch("topup_checkout.client_id_from_user_id", return_value=CLIENT_ID)
def test_inactive_subscriber_returns_403(
    mock_client_id: MagicMock,
    mock_entitlement: MagicMock,
) -> None:
    with pytest.raises(TopupError) as exc:
        process_topup(USER_ID, "100")
    assert exc.value.status == 403


def test_unauthenticated_returns_401() -> None:
    with pytest.raises(TopupError) as exc:
        process_topup(None, "100")
    assert exc.value.status == 401


@patch("entitlements.rest_patch_filter")
@patch("entitlements.get_entitlement", return_value=ACTIVE_ENTITLEMENT)
def test_apply_topup_increases_actions_limit(
    mock_get_entitlement: MagicMock,
    mock_patch: MagicMock,
) -> None:
    apply_topup(CLIENT_ID, 100)
    mock_patch.assert_called_once()
    payload = mock_patch.call_args.args[2]
    assert payload["actions_limit"] == 600


@patch("entitlements.rest_patch_filter")
@patch("entitlements.get_entitlement", return_value={**ACTIVE_ENTITLEMENT, "status": "inactive"})
def test_apply_topup_inactive_client_no_op(
    mock_get_entitlement: MagicMock,
    mock_patch: MagicMock,
) -> None:
    apply_topup(CLIENT_ID, 100)
    mock_patch.assert_not_called()


@patch("billing_webhook.apply_topup")
def test_checkout_completed_routes_topup_when_payment_mode(
    mock_apply_topup: MagicMock,
) -> None:
    event = {
        "data": {
            "object": {
                "client_reference_id": CLIENT_ID,
                "subscription": None,
                "mode": "payment",
                "metadata": {"topup_actions": "500", "pack_size": "500"},
            }
        }
    }
    handle_checkout_session_completed(event)
    mock_apply_topup.assert_called_once_with(CLIENT_ID, 500)


@patch("billing_webhook.apply_topup")
@patch("billing_webhook._apply_active_subscription")
@patch("billing_webhook._fetch_subscription")
def test_checkout_completed_routes_subscription_when_present(
    mock_fetch_subscription: MagicMock,
    mock_apply_subscription: MagicMock,
    mock_apply_topup: MagicMock,
) -> None:
    subscription = {"id": "sub_123", "status": "active"}
    mock_fetch_subscription.return_value = subscription
    event = {
        "data": {
            "object": {
                "client_reference_id": CLIENT_ID,
                "subscription": "sub_123",
                "mode": "subscription",
                "metadata": {},
            }
        }
    }
    handle_checkout_session_completed(event)
    mock_fetch_subscription.assert_called_once_with("sub_123")
    mock_apply_subscription.assert_called_once_with(CLIENT_ID, subscription)
    mock_apply_topup.assert_not_called()
