"""Stripe Customer Portal session creation."""

from __future__ import annotations

from typing import Any, Dict, Optional

from billing_auth import BillingAuthError, require_billing_owner
from entitlements import get_entitlement
from stripe_client import get_stripe

PORTAL_RETURN_URL = "https://app.logiqops.co.uk/billing/success"


class PortalError(Exception):
    def __init__(self, status: int, payload: Dict[str, Any]):
        super().__init__(payload.get("message") or payload.get("detail") or str(payload))
        self.status = status
        self.payload = payload


def process_portal(user_id: Optional[str]) -> Dict[str, str]:
    try:
        client_id = require_billing_owner(user_id)
    except BillingAuthError as exc:
        raise PortalError(exc.status, {"detail": exc.detail}) from exc

    entitlement = get_entitlement(client_id)
    status = (entitlement.get("status") or "").strip().lower() if entitlement else ""
    customer_id = (entitlement.get("stripe_customer_id") or "").strip() if entitlement else ""

    if status != "active" or not customer_id:
        raise PortalError(
            403,
            {
                "error": "no_active_subscription",
                "message": "Please subscribe first.",
            },
        )

    stripe = get_stripe()
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=PORTAL_RETURN_URL,
    )
    if not session.url:
        raise PortalError(502, {"detail": "Stripe did not return a portal URL"})
    return {"url": session.url}
