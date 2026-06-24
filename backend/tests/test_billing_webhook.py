"""Tests for Stripe billing webhooks and entitlements sync."""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

# Fixed test secrets — set before billing_webhook import (_load_webhook_secret runs at import).
TEST_STRIPE_SECRET_KEY = "sk_test_dummy_key_for_unit_tests"
TEST_WEBHOOK_SECRET = "whsec_test_dummy_for_unit_tests"

_ROOT = Path(__file__).resolve().parent.parent.parent
_API_LIB = _ROOT / "api_lib"
_BACKEND = _ROOT / "backend"
sys.path.insert(0, str(_API_LIB))
sys.path.insert(0, str(_BACKEND))

os.environ["STRIPE_SECRET_KEY"] = TEST_STRIPE_SECRET_KEY
os.environ["STRIPE_WEBHOOK_SECRET"] = TEST_WEBHOOK_SECRET
os.environ["STRIPE_PRICE_SPARK"] = "price_test_spark"
os.environ["STRIPE_PRICE_STARTER"] = "price_test_starter"
os.environ["STRIPE_PRICE_PRO"] = "price_test_pro"
os.environ["STRIPE_PRICE_BUSINESS"] = "price_test_business"

import billing_webhook  # noqa: E402
import tiers  # noqa: E402
from billing_webhook import (  # noqa: E402
    WebhookError,
    _entitlement_payload_from_subscription,
    _unix_to_iso,
    handle_checkout_session_completed,
    handle_invoice_payment_failed,
    handle_subscription_created,
    handle_subscription_deleted,
    handle_subscription_updated,
    process_event,
    verify_event,
)
from tiers import tier_from_price_id  # noqa: E402

CLIENT_ID = "11111111-2222-4333-8444-555555555555"
SUBSCRIPTION_ID = "sub_test_123"
CUSTOMER_ID = "cus_test_456"
ITEM_PERIOD_START = 1700000000
ITEM_PERIOD_END = 1703000000


def _subscription_object(
    *,
    price_id: str = "price_test_starter",
    status: str = "active",
    client_id: str = CLIENT_ID,
    top_level_periods: bool = True,
    item_periods: bool = False,
) -> Dict[str, Any]:
    item: Dict[str, Any] = {"price": {"id": price_id}}
    if item_periods:
        item["current_period_start"] = ITEM_PERIOD_START
        item["current_period_end"] = ITEM_PERIOD_END
    sub: Dict[str, Any] = {
        "id": SUBSCRIPTION_ID,
        "customer": CUSTOMER_ID,
        "status": status,
        "metadata": {"client_id": client_id},
        "items": {"data": [item]},
    }
    if top_level_periods:
        sub["current_period_start"] = ITEM_PERIOD_START
        sub["current_period_end"] = ITEM_PERIOD_END
    else:
        sub["current_period_start"] = None
        sub["current_period_end"] = None
    return sub


def _event(event_type: str, obj: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": event_type, "data": {"object": obj}}


@patch("billing_webhook.get_stripe")
def test_verify_event_valid_signature(mock_get_stripe: MagicMock) -> None:
    payload = json.dumps({"id": "evt_test", "type": "ping"}).encode("utf-8")
    mock_stripe = MagicMock()
    mock_stripe.Webhook.construct_event.return_value = {"id": "evt_test", "type": "ping"}
    mock_get_stripe.return_value = mock_stripe

    event = verify_event(payload, "valid-signature")
    assert event["id"] == "evt_test"
    mock_stripe.Webhook.construct_event.assert_called_once_with(
        payload,
        "valid-signature",
        TEST_WEBHOOK_SECRET,
    )


def test_verify_event_invalid_signature_raises() -> None:
    payload = b'{"id": "evt_test"}'
    with pytest.raises(ValueError, match="signature verification failed"):
        verify_event(payload, "invalid")


def test_tier_from_price_id_resolves_known_prices() -> None:
    assert tier_from_price_id("price_test_spark") == "spark"
    assert tier_from_price_id("price_test_starter") == "starter"
    assert tier_from_price_id("price_test_pro") == "pro"
    assert tier_from_price_id("price_test_business") == "business"
    assert tier_from_price_id("price_unknown") is None


def test_period_dates_read_from_item_when_top_level_missing() -> None:
    subscription = _subscription_object(top_level_periods=False, item_periods=True)
    payload = _entitlement_payload_from_subscription(CLIENT_ID, subscription)
    assert payload is not None
    assert payload["current_period_start"] == _unix_to_iso(ITEM_PERIOD_START)
    assert payload["current_period_end"] == _unix_to_iso(ITEM_PERIOD_END)


@patch("billing_webhook.sync_user_profiles_plan")
@patch("billing_webhook.upsert_entitlement")
@patch("billing_webhook._fetch_subscription")
def test_handle_checkout_session_completed(
    mock_fetch: MagicMock,
    mock_upsert: MagicMock,
    mock_sync_profiles: MagicMock,
) -> None:
    mock_fetch.return_value = _subscription_object()
    handle_checkout_session_completed(
        _event(
            "checkout.session.completed",
            {
                "client_reference_id": CLIENT_ID,
                "subscription": SUBSCRIPTION_ID,
            },
        )
    )
    mock_fetch.assert_called_once_with(SUBSCRIPTION_ID)
    upsert_payload = mock_upsert.call_args.args[0]
    assert upsert_payload["client_id"] == CLIENT_ID
    assert upsert_payload["plan"] == "starter"
    assert upsert_payload["status"] == "active"
    mock_sync_profiles.assert_called_once_with(CLIENT_ID, "starter")


@patch("billing_webhook.sync_user_profiles_plan")
@patch("billing_webhook.upsert_entitlement")
def test_handle_subscription_created(
    mock_upsert: MagicMock,
    mock_sync_profiles: MagicMock,
) -> None:
    handle_subscription_created(
        _event("customer.subscription.created", _subscription_object(price_id="price_test_pro"))
    )
    upsert_payload = mock_upsert.call_args.args[0]
    assert upsert_payload["plan"] == "pro"
    mock_sync_profiles.assert_called_once_with(CLIENT_ID, "pro")


@patch("billing_webhook.sync_user_profiles_plan")
@patch("billing_webhook.upsert_entitlement")
@patch("billing_webhook.get_entitlement_by_subscription_id")
def test_handle_subscription_updated(
    mock_get_by_sub: MagicMock,
    mock_upsert: MagicMock,
    mock_sync_profiles: MagicMock,
) -> None:
    mock_get_by_sub.return_value = {"client_id": CLIENT_ID}
    handle_subscription_updated(
        _event("customer.subscription.updated", _subscription_object(price_id="price_test_business"))
    )
    upsert_payload = mock_upsert.call_args.args[0]
    assert upsert_payload["plan"] == "business"
    mock_sync_profiles.assert_called_once_with(CLIENT_ID, "business")


@patch("billing_webhook.sync_user_profiles_plan")
@patch("billing_webhook.upsert_entitlement")
@patch("billing_webhook.get_entitlement_by_subscription_id")
def test_handle_subscription_deleted(
    mock_get_by_sub: MagicMock,
    mock_upsert: MagicMock,
    mock_sync_profiles: MagicMock,
) -> None:
    # subscription.deleted sets status='canceled' even if the row was bootstrapped
    # as 'inactive' — in practice only fires after subscription.created.
    mock_get_by_sub.return_value = {"client_id": CLIENT_ID}
    handle_subscription_deleted(
        _event("customer.subscription.deleted", {"id": SUBSCRIPTION_ID})
    )
    upsert_payload = mock_upsert.call_args.args[0]
    assert upsert_payload["status"] == "canceled"
    assert upsert_payload["plan"] is None
    assert upsert_payload["actions_limit"] == 0
    mock_sync_profiles.assert_called_once_with(CLIENT_ID, None)


@patch("billing_webhook.sync_user_profiles_plan")
@patch("billing_webhook.upsert_entitlement")
@patch("billing_webhook.get_entitlement_by_subscription_id")
def test_handle_invoice_payment_failed(
    mock_get_by_sub: MagicMock,
    mock_upsert: MagicMock,
    mock_sync_profiles: MagicMock,
) -> None:
    mock_get_by_sub.return_value = {
        "client_id": CLIENT_ID,
        "plan": "starter",
        "status": "active",
    }
    handle_invoice_payment_failed(
        _event("invoice.payment_failed", {"subscription": SUBSCRIPTION_ID})
    )
    upsert_payload = mock_upsert.call_args.args[0]
    assert upsert_payload == {"client_id": CLIENT_ID, "status": "past_due"}
    mock_sync_profiles.assert_not_called()


@patch("billing_webhook.sync_user_profiles_plan")
@patch("billing_webhook.upsert_entitlement")
@patch("billing_webhook._fetch_subscription")
def test_checkout_completed_idempotent(
    mock_fetch: MagicMock,
    mock_upsert: MagicMock,
    mock_sync_profiles: MagicMock,
) -> None:
    mock_fetch.return_value = _subscription_object()
    event = _event(
        "checkout.session.completed",
        {"client_reference_id": CLIENT_ID, "subscription": SUBSCRIPTION_ID},
    )
    handle_checkout_session_completed(event)
    first_payload = mock_upsert.call_args.args[0]
    handle_checkout_session_completed(event)
    second_payload = mock_upsert.call_args.args[0]
    assert first_payload["client_id"] == second_payload["client_id"]
    assert first_payload["plan"] == second_payload["plan"]
    assert first_payload["status"] == second_payload["status"]
    assert first_payload["stripe_subscription_id"] == second_payload["stripe_subscription_id"]
    assert mock_upsert.call_count == 2
    assert mock_sync_profiles.call_count == 2


@patch("billing_webhook.upsert_entitlement")
@patch("billing_webhook.verify_event")
def test_unknown_event_returns_received_without_db_change(
    mock_verify: MagicMock,
    mock_upsert: MagicMock,
) -> None:
    mock_verify.return_value = {"type": "customer.created", "data": {"object": {}}}
    result = process_event(b"{}", "sig")
    assert result == {"received": True}
    mock_upsert.assert_not_called()


def test_process_event_invalid_signature_returns_400() -> None:
    with pytest.raises(WebhookError) as exc:
        process_event(b"{}", "not-a-valid-signature")
    assert exc.value.status == 400


def test_stripe_webhook_secret_missing_raises_at_import() -> None:
    original = os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
    try:
        with pytest.raises(RuntimeError, match="STRIPE_WEBHOOK_SECRET"):
            importlib.reload(billing_webhook)
    finally:
        os.environ["STRIPE_WEBHOOK_SECRET"] = TEST_WEBHOOK_SECRET
        importlib.reload(billing_webhook)
