"""Stripe Checkout session creation for subscription signups (step 3 — payment only)."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from billing_auth import BillingAuthError, require_billing_owner
from entitlements import get_entitlement
from stripe_client import get_stripe

VALID_TIERS = frozenset({"spark", "starter", "pro", "business"})

TIER_PRICE_ENV: Dict[str, str] = {
    "spark": "STRIPE_PRICE_SPARK",
    "starter": "STRIPE_PRICE_STARTER",
    "pro": "STRIPE_PRICE_PRO",
    "business": "STRIPE_PRICE_BUSINESS",
}

CHECKOUT_SUCCESS_URL = (
    "https://app.logiqops.co.uk/billing/success?session_id={CHECKOUT_SESSION_ID}"
)
CHECKOUT_CANCEL_URL = "https://app.logiqops.co.uk/billing/cancel"


class CheckoutError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def price_id_for_tier(tier: str) -> Optional[str]:
    env_name = TIER_PRICE_ENV.get(tier)
    if not env_name:
        return None
    return (os.environ.get(env_name) or "").strip() or None


def create_checkout_session(client_id: str, tier: str) -> str:
    normalized = tier.lower().strip()
    if normalized not in VALID_TIERS:
        raise CheckoutError(400, f"Invalid tier '{tier}' — must be one of: spark, starter, pro, business")

    price_id = price_id_for_tier(normalized)
    if not price_id:
        raise CheckoutError(400, f"Stripe price not configured for tier '{normalized}'")

    stripe = get_stripe()
    entitlement = get_entitlement(client_id)
    customer_id = (
        (entitlement.get("stripe_customer_id") or "").strip() if entitlement else ""
    )
    create_kwargs: Dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "client_reference_id": client_id,
        "metadata": {"client_id": client_id},
        "subscription_data": {"metadata": {"client_id": client_id}},
        "allow_promotion_codes": True,
        "success_url": CHECKOUT_SUCCESS_URL,
        "cancel_url": CHECKOUT_CANCEL_URL,
        "automatic_tax": {"enabled": False},
    }
    if customer_id:
        create_kwargs["customer"] = customer_id
    session = stripe.checkout.Session.create(**create_kwargs)
    if not session.url:
        raise CheckoutError(502, "Stripe did not return a checkout URL")
    return session.url


def process_checkout(user_id: Optional[str], tier: Optional[str]) -> Dict[str, str]:
    if not user_id:
        raise CheckoutError(401, "Not authenticated")

    tier_value = (tier or "").strip()
    if not tier_value:
        raise CheckoutError(400, "tier is required")

    try:
        client_id = require_billing_owner(user_id)
    except BillingAuthError as exc:
        raise CheckoutError(exc.status, exc.detail) from exc

    url = create_checkout_session(client_id, tier_value)
    return {"url": url}
