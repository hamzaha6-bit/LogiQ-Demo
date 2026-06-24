# MIRROR of api_lib/tiers.py — keep in sync.
"""Tier limits and Stripe price → tier resolution."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

# TIER_LIMITS — provisional. To be tuned based on observed usage and Claude cost
# once we have real client data. Update via separate PR; downstream code reads
# whatever is set here.
TIER_LIMITS: Dict[str, Dict[str, int]] = {
    "spark": {"actions": 50, "agents": 1, "workflows": 1, "spend_cap_pence": 800},
    "starter": {"actions": 500, "agents": 1, "workflows": 2, "spend_cap_pence": 4000},
    "pro": {"actions": 2500, "agents": 3, "workflows": 10, "spend_cap_pence": 12000},
    "business": {"actions": 10000, "agents": 999999, "workflows": 999999, "spend_cap_pence": 40000},
}

TIER_PRICE_ENV: Dict[str, str] = {
    "spark": "STRIPE_PRICE_SPARK",
    "starter": "STRIPE_PRICE_STARTER",
    "pro": "STRIPE_PRICE_PRO",
    "business": "STRIPE_PRICE_BUSINESS",
}

_ZERO_LIMITS = {"actions": 0, "agents": 0, "workflows": 0, "spend_cap_pence": 0}


def limits_for(tier: Optional[str]) -> Dict[str, int]:
    if not tier:
        return dict(_ZERO_LIMITS)
    normalized = tier.lower().strip()
    limits = TIER_LIMITS.get(normalized)
    if not limits:
        return dict(_ZERO_LIMITS)
    return dict(limits)


def tier_from_price_id(price_id: Optional[str]) -> Optional[str]:
    pid = (price_id or "").strip()
    if not pid:
        return None
    for tier, env_name in TIER_PRICE_ENV.items():
        if (os.environ.get(env_name) or "").strip() == pid:
            return tier
    return None
