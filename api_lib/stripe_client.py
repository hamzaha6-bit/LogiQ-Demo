"""Stripe SDK wrapper for Vercel API functions."""

from __future__ import annotations

import os

import stripe


def _load_api_key() -> str:
    raw = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
    if not raw:
        raise RuntimeError(
            "STRIPE_SECRET_KEY is not set — required for Stripe billing checkout"
        )
    return raw


_API_KEY = _load_api_key()
stripe.api_key = _API_KEY


def get_stripe():
    """Return the configured Stripe module (api_key already set)."""
    return stripe
