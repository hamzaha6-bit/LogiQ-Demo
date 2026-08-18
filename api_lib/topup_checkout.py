"""Stripe Checkout for one-off action top-up packs."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from billing_auth import BillingAuthError, require_billing_owner
from entitlements import get_entitlement
from stripe_client import get_stripe

TOPUP_PACKS: Dict[str, Dict[str, Any]] = {
    "100": {"actions": 100, "price_env": "STRIPE_TOPUP_PRICE_100"},
    "500": {"actions": 500, "price_env": "STRIPE_TOPUP_PRICE_500"},
}

CHECKOUT_SUCCESS_URL = (
    "https://app.logiqops.co.uk/billing/success?session_id={CHECKOUT_SESSION_ID}"
)
CHECKOUT_CANCEL_URL = "https://app.logiqops.co.uk/billing/cancel"


class TopupError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def price_id_for_pack(pack_size: str) -> Optional[str]:
    pack = TOPUP_PACKS.get(pack_size)
    if not pack:
        return None
    env_name = pack["price_env"]
    return (os.environ.get(env_name) or "").strip() or None


def create_topup_session(client_id: str, pack_size: str) -> str:
    pack = TOPUP_PACKS.get(pack_size)
    if not pack:
        raise TopupError(400, f"Invalid pack_size '{pack_size}' — must be one of: 100, 500")

    price_id = price_id_for_pack(pack_size)
    if not price_id:
        raise TopupError(400, f"Stripe price not configured for pack '{pack_size}'")

    actions = int(pack["actions"])
    stripe = get_stripe()
    create_kwargs: Dict[str, Any] = {
        "mode": "payment",
        "line_items": [{"price": price_id, "quantity": 1}],
        "client_reference_id": client_id,
        "metadata": {
            "topup_actions": str(actions),
            "pack_size": pack_size,
            "client_id": client_id,
        },
        "success_url": CHECKOUT_SUCCESS_URL,
        "cancel_url": CHECKOUT_CANCEL_URL,
        "automatic_tax": {"enabled": False},
    }
    entitlement = get_entitlement(client_id)
    customer_id = (
        (entitlement.get("stripe_customer_id") or "").strip() if entitlement else ""
    )
    if customer_id:
        create_kwargs["customer"] = customer_id
    session = stripe.checkout.Session.create(**create_kwargs)
    if not session.url:
        raise TopupError(502, "Stripe did not return a checkout URL")
    return session.url


def process_topup(user_id: Optional[str], pack_size: Optional[str]) -> Dict[str, str]:
    if not user_id:
        raise TopupError(401, "Not authenticated")

    pack_value = (pack_size or "").strip()
    if pack_value not in TOPUP_PACKS:
        raise TopupError(400, f"Invalid pack_size '{pack_size}' — must be one of: 100, 500")

    try:
        client_id = require_billing_owner(user_id)
    except BillingAuthError as exc:
        raise TopupError(exc.status, exc.detail) from exc

    entitlement = get_entitlement(client_id)
    status = (entitlement.get("status") or "").strip().lower() if entitlement else ""
    if status != "active":
        raise TopupError(403, "An active subscription is required to purchase top-ups")

    url = create_topup_session(client_id, pack_value)
    return {"url": url}
