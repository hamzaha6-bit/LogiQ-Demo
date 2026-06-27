"""Stripe billing webhook verification and event handlers."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from entitlements import (
    apply_topup,
    get_entitlement_by_subscription_id,
    sync_user_profiles_plan,
    upsert_entitlement,
)
from stripe_client import get_stripe
from tiers import limits_for, tier_from_price_id

logger = logging.getLogger("logiq.billing_webhook")


def _load_webhook_secret() -> str:
    raw = (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()
    if not raw:
        raise RuntimeError(
            "STRIPE_WEBHOOK_SECRET is not set — required for Stripe webhook verification"
        )
    return raw


_WEBHOOK_SECRET = _load_webhook_secret()


class WebhookError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def verify_event(payload: bytes, sig_header: str):
    stripe = get_stripe()
    try:
        return stripe.Webhook.construct_event(payload, sig_header, _WEBHOOK_SECRET)
    except Exception as exc:
        raise ValueError(f"Stripe webhook signature verification failed: {exc}") from exc


def _as_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict_recursive"):
        return obj.to_dict_recursive()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return dict(obj)


def _unix_to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _price_id_from_subscription(subscription: Dict[str, Any]) -> Optional[str]:
    items = subscription.get("items") or {}
    data = items.get("data") if isinstance(items, dict) else getattr(items, "data", [])
    if not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        first = _as_dict(first)
    price = first.get("price")
    if not isinstance(price, dict):
        price = _as_dict(price)
    return (price.get("id") or "").strip() or None


def _read_period_dates(subscription: Dict[str, Any]) -> tuple[Any, Any]:
    """Read billing period from subscription item (Stripe 2024+) with top-level fallback."""
    items = subscription.get("items") or {}
    data = items.get("data") if isinstance(items, dict) else getattr(items, "data", [])
    if data:
        first = data[0] if isinstance(data[0], dict) else _as_dict(data[0])
        start = first.get("current_period_start") or subscription.get("current_period_start")
        end = first.get("current_period_end") or subscription.get("current_period_end")
        return start, end
    return subscription.get("current_period_start"), subscription.get("current_period_end")


def _entitlement_status_from_subscription(stripe_status: str) -> str:
    normalized = (stripe_status or "").lower().strip()
    if normalized in {"active", "trialing"}:
        return "active"
    if normalized == "past_due":
        return "past_due"
    if normalized in {"canceled", "unpaid", "incomplete_expired"}:
        return "canceled"
    return "inactive"


def _inactive_entitlement_payload(client_id: str) -> Dict[str, Any]:
    # status='canceled' is correct for subscription.deleted (created → deleted path).
    # Edge case: a bootstrapped 'inactive' row with no prior subscription would also
    # become 'canceled' here — acceptable for now; created → deleted is the only
    # realistic webhook path in production.
    limits = limits_for(None)
    return {
        "client_id": client_id,
        "plan": None,
        "status": "canceled",
        "stripe_customer_id": None,
        "stripe_subscription_id": None,
        "current_period_start": None,
        "current_period_end": None,
        "actions_limit": limits["actions"],
        "agents_limit": limits["agents"],
        "workflows_limit": limits["workflows"],
        "spend_cap_pence": limits["spend_cap_pence"],
    }


def _entitlement_payload_from_subscription(
    client_id: str,
    subscription: Dict[str, Any],
    *,
    status_override: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    price_id = _price_id_from_subscription(subscription)
    tier = tier_from_price_id(price_id)
    if not tier:
        logger.warning("Unknown Stripe price id %s for client %s", price_id, client_id)
        return None

    limits = limits_for(tier)
    status = status_override or _entitlement_status_from_subscription(
        str(subscription.get("status") or "")
    )
    customer = subscription.get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")

    period_start, period_end = _read_period_dates(subscription)

    return {
        "client_id": client_id,
        "plan": tier,
        "status": status,
        "stripe_customer_id": str(customer) if customer else None,
        "stripe_subscription_id": subscription.get("id"),
        "current_period_start": _unix_to_iso(period_start),
        "current_period_end": _unix_to_iso(period_end),
        "actions_limit": limits["actions"],
        "agents_limit": limits["agents"],
        "workflows_limit": limits["workflows"],
        "spend_cap_pence": limits["spend_cap_pence"],
    }


def _apply_active_subscription(client_id: str, subscription: Dict[str, Any]) -> None:
    payload = _entitlement_payload_from_subscription(client_id, subscription)
    if not payload:
        return
    upsert_entitlement(payload)
    sync_user_profiles_plan(client_id, payload["plan"])
    try:
        from hook_handler import send_subscription_confirmation

        send_subscription_confirmation(client_id, str(payload["plan"]))
    except Exception as exc:
        logger.warning(
            "Subscription confirmation email failed for client %s: %s",
            client_id,
            exc,
        )


def _fetch_subscription(subscription_id: str) -> Dict[str, Any]:
    stripe = get_stripe()
    return _as_dict(stripe.Subscription.retrieve(subscription_id))


def handle_checkout_session_completed(event: Any) -> None:
    session = _as_dict(event["data"]["object"])
    client_id = (session.get("client_reference_id") or "").strip()
    if not client_id:
        logger.warning("checkout.session.completed missing client_reference_id")
        return

    subscription_id = session.get("subscription")
    if subscription_id:
        subscription = _fetch_subscription(str(subscription_id))
        _apply_active_subscription(client_id, subscription)
        return

    metadata = session.get("metadata") or {}
    mode = (session.get("mode") or "").strip().lower()
    topup_actions = metadata.get("topup_actions")
    if mode == "payment" and topup_actions:
        try:
            actions_to_add = int(topup_actions)
        except (TypeError, ValueError):
            logger.warning(
                "checkout.session.completed invalid topup_actions for client %s",
                client_id,
            )
            return
        apply_topup(client_id, actions_to_add)
        return

    logger.warning("checkout.session.completed unhandled session for client %s", client_id)


def handle_subscription_created(event: Any) -> None:
    subscription = _as_dict(event["data"]["object"])
    client_id = (subscription.get("metadata") or {}).get("client_id")
    if not client_id:
        existing = get_entitlement_by_subscription_id(str(subscription.get("id") or ""))
        if existing:
            client_id = existing.get("client_id")
    if not client_id:
        logger.warning("subscription.created could not resolve client_id for %s", subscription.get("id"))
        return
    _apply_active_subscription(str(client_id), subscription)


def handle_subscription_updated(event: Any) -> None:
    subscription = _as_dict(event["data"]["object"])
    subscription_id = str(subscription.get("id") or "")
    existing = get_entitlement_by_subscription_id(subscription_id)
    client_id = (existing or {}).get("client_id")
    if not client_id:
        client_id = (subscription.get("metadata") or {}).get("client_id")
    if not client_id:
        logger.warning("subscription.updated could not resolve client_id for %s", subscription_id)
        return
    _apply_active_subscription(str(client_id), subscription)


def handle_subscription_deleted(event: Any) -> None:
    subscription = _as_dict(event["data"]["object"])
    subscription_id = str(subscription.get("id") or "")
    existing = get_entitlement_by_subscription_id(subscription_id)
    client_id = (existing or {}).get("client_id")
    if not client_id:
        client_id = (subscription.get("metadata") or {}).get("client_id")
    if not client_id:
        logger.warning("subscription.deleted could not resolve client_id for %s", subscription_id)
        return

    upsert_entitlement(_inactive_entitlement_payload(str(client_id)))
    sync_user_profiles_plan(str(client_id), None)


def handle_invoice_payment_failed(event: Any) -> None:
    invoice = _as_dict(event["data"]["object"])
    subscription_id = invoice.get("subscription")
    if not subscription_id:
        logger.warning("invoice.payment_failed missing subscription id")
        return

    existing = get_entitlement_by_subscription_id(str(subscription_id))
    if not existing:
        logger.warning("invoice.payment_failed no entitlement for subscription %s", subscription_id)
        return

    upsert_entitlement(
        {
            "client_id": existing["client_id"],
            "status": "past_due",
        }
    )


_EVENT_HANDLERS: Dict[str, Callable[[Any], None]] = {
    "checkout.session.completed": handle_checkout_session_completed,
    "customer.subscription.created": handle_subscription_created,
    "customer.subscription.updated": handle_subscription_updated,
    "customer.subscription.deleted": handle_subscription_deleted,
    "invoice.payment_failed": handle_invoice_payment_failed,
}


def process_event(payload: bytes, sig_header: str) -> Dict[str, bool]:
    try:
        event = verify_event(payload, sig_header)
    except ValueError as exc:
        raise WebhookError(400, str(exc)) from exc

    event_type = event["type"]
    handler = _EVENT_HANDLERS.get(event_type)
    if handler:
        handler(event)
    else:
        logger.info("Ignoring unhandled Stripe event type: %s", event_type)

    return {"received": True}
