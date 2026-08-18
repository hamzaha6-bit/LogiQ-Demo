"""Tests for Stripe Checkout subscription signup (step 3 — payment only)."""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent.parent
_API_LIB = _ROOT / "api_lib"
_BACKEND = _ROOT / "backend"
sys.path.insert(0, str(_API_LIB))
sys.path.insert(0, str(_BACKEND))
load_dotenv(_BACKEND / ".env")

os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy_key_for_unit_tests")
os.environ.setdefault("STRIPE_PRICE_SPARK", "price_test_spark")
os.environ.setdefault("STRIPE_PRICE_STARTER", "price_test_starter")
os.environ.setdefault("STRIPE_PRICE_PRO", "price_test_pro")
os.environ.setdefault("STRIPE_PRICE_BUSINESS", "price_test_business")

import billing_checkout  # noqa: E402
import stripe_client  # noqa: E402
from billing_auth import BillingAuthError, require_billing_owner  # noqa: E402
from billing_checkout import CheckoutError, create_checkout_session, process_checkout  # noqa: E402

KNOWN_USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
KNOWN_CLIENT_ID = "11111111-2222-4333-8444-555555555555"


@patch("billing_checkout.get_entitlement", return_value=None)
@patch("billing_checkout.get_stripe")
@patch("billing_checkout.require_billing_owner", return_value=KNOWN_CLIENT_ID)
def test_tier_spark_returns_checkout_url(mock_owner, mock_get_stripe, mock_entitlement) -> None:
    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/c/pay/cs_test_abc123"
    mock_get_stripe.return_value.checkout.Session.create.return_value = mock_session

    result = process_checkout(KNOWN_USER_ID, "spark")

    assert result["url"].startswith("https://checkout.stripe.com/")
    create_kwargs = mock_get_stripe.return_value.checkout.Session.create.call_args.kwargs
    assert create_kwargs["client_reference_id"] == KNOWN_CLIENT_ID
    assert create_kwargs["mode"] == "subscription"
    assert create_kwargs["allow_promotion_codes"] is True
    assert create_kwargs["automatic_tax"] == {"enabled": False}
    assert create_kwargs["metadata"]["client_id"] == KNOWN_CLIENT_ID
    assert create_kwargs["subscription_data"]["metadata"]["client_id"] == KNOWN_CLIENT_ID


@patch("billing_checkout.get_stripe")
@patch("billing_checkout.require_billing_owner", return_value=KNOWN_CLIENT_ID)
def test_invalid_tier_returns_400(mock_owner, mock_get_stripe) -> None:
    with pytest.raises(CheckoutError) as exc:
        process_checkout(KNOWN_USER_ID, "concierge")
    assert exc.value.status == 400
    mock_get_stripe.return_value.checkout.Session.create.assert_not_called()


def test_missing_tier_returns_400() -> None:
    with pytest.raises(CheckoutError) as exc:
        process_checkout(KNOWN_USER_ID, "")
    assert exc.value.status == 400
    assert "tier is required" in exc.value.detail


def test_unauthenticated_returns_401() -> None:
    with pytest.raises(CheckoutError) as exc:
        process_checkout(None, "spark")
    assert exc.value.status == 401


@patch("billing_checkout.get_entitlement", return_value=None)
@patch("billing_checkout.get_stripe")
def test_client_reference_id_matches_client_id(mock_get_stripe, mock_entitlement) -> None:
    client_id = str(uuid.uuid4())
    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/c/pay/cs_test_xyz"
    mock_get_stripe.return_value.checkout.Session.create.return_value = mock_session

    create_checkout_session(client_id, "starter")

    create_kwargs = mock_get_stripe.return_value.checkout.Session.create.call_args.kwargs
    assert create_kwargs["client_reference_id"] == client_id
    assert create_kwargs["metadata"]["client_id"] == client_id
    assert create_kwargs["subscription_data"]["metadata"]["client_id"] == client_id
    assert create_kwargs["line_items"][0]["quantity"] == 1
    assert create_kwargs["line_items"][0]["price"].startswith("price_")


@patch("billing_auth.rest_get")
def test_require_billing_owner_returns_owner_client(mock_rest_get) -> None:
    mock_rest_get.return_value = [
        {"client_id": KNOWN_CLIENT_ID, "role": "owner"},
    ]
    result = require_billing_owner(KNOWN_USER_ID)
    assert result == KNOWN_CLIENT_ID
    uuid.UUID(result)


@patch("billing_auth.rest_get", return_value=[{"client_id": KNOWN_CLIENT_ID, "role": "member"}])
def test_require_billing_owner_rejects_member(mock_rest_get) -> None:
    with pytest.raises(BillingAuthError) as exc:
        require_billing_owner(KNOWN_USER_ID)
    assert exc.value.status == 403


@patch("billing_checkout.require_billing_owner", side_effect=BillingAuthError(403, "Only the client owner can manage billing"))
def test_member_cannot_start_checkout(mock_owner) -> None:
    with pytest.raises(CheckoutError) as exc:
        process_checkout(KNOWN_USER_ID, "spark")
    assert exc.value.status == 403


@patch("billing_auth.rest_get", return_value=[])
def test_require_billing_owner_raises_when_no_membership(mock_rest_get) -> None:
    fake_user = "00000000-0000-4000-8000-000000099999"
    with pytest.raises(BillingAuthError) as exc:
        require_billing_owner(fake_user)
    assert exc.value.status == 400


def test_stripe_secret_key_missing_raises_at_import() -> None:
    original = os.environ.pop("STRIPE_SECRET_KEY", None)
    try:
        with pytest.raises(RuntimeError, match="STRIPE_SECRET_KEY"):
            importlib.reload(stripe_client)
    finally:
        if original is not None:
            os.environ["STRIPE_SECRET_KEY"] = original
        else:
            os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy_key_for_unit_tests"
        importlib.reload(stripe_client)
        importlib.reload(billing_checkout)
