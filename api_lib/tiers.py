"""Tier limits and Stripe price → tier resolution."""

from __future__ import annotations

import os
from typing import Dict, Optional

# TIER_LIMITS — provisional. To be tuned based on observed usage and Claude cost
# once we have real client data. Update via separate PR; downstream code reads
# whatever is set here.
#
# agents / active_agents_limit: max concurrently active agents (client_agents.status='active').
# 0 = unlimited (enterprise / special); owner emails bypass the gate entirely.
TIER_LIMITS: Dict[str, Dict[str, int]] = {
    "spark": {
        "actions": 50,
        "agents": 1,
        "active_agents_limit": 1,
        "workflows": 1,
        "spend_cap_pence": 800,
    },
    "starter": {
        "actions": 500,
        "agents": 2,
        "active_agents_limit": 2,
        "workflows": 2,
        "spend_cap_pence": 4000,
    },
    "pro": {
        "actions": 2500,
        "agents": 3,
        "active_agents_limit": 3,
        "workflows": 10,
        "spend_cap_pence": 12000,
    },
    "business": {
        "actions": 10000,
        "agents": 5,
        "active_agents_limit": 5,
        "workflows": 999999,
        "spend_cap_pence": 40000,
    },
    # Enterprise: unlimited agents (0 sentinel). Not yet in Stripe price map.
    "enterprise": {
        "actions": 999999,
        "agents": 0,
        "active_agents_limit": 0,
        "workflows": 0,
        "spend_cap_pence": 0,
    },
}

TIER_PRICE_ENV: Dict[str, str] = {
    "spark": "STRIPE_PRICE_SPARK",
    "starter": "STRIPE_PRICE_STARTER",
    "pro": "STRIPE_PRICE_PRO",
    "business": "STRIPE_PRICE_BUSINESS",
}

_ZERO_LIMITS = {
    "actions": 0,
    "agents": 0,
    "active_agents_limit": 0,
    "workflows": 0,
    "spend_cap_pence": 0,
}

# Next tier that unlocks more active agent slots (for upgrade prompts).
TIER_AGENT_UPGRADE: Dict[str, str] = {
    "spark": "starter",
    "starter": "pro",
    "pro": "business",
    "business": "enterprise",
}


def limits_for(tier: Optional[str]) -> Dict[str, int]:
    if not tier:
        return dict(_ZERO_LIMITS)
    normalized = tier.lower().strip()
    limits = TIER_LIMITS.get(normalized)
    if not limits:
        return dict(_ZERO_LIMITS)
    out = dict(limits)
    # Keep agents and active_agents_limit aligned when only one is set.
    if "active_agents_limit" not in out and "agents" in out:
        out["active_agents_limit"] = out["agents"]
    if "agents" not in out and "active_agents_limit" in out:
        out["agents"] = out["active_agents_limit"]
    return out


def active_agents_limit_for(tier: Optional[str]) -> int:
    """Max active agents; 0 means unlimited."""
    return int(limits_for(tier).get("active_agents_limit") or limits_for(tier).get("agents") or 0)


def upgrade_tier_for_agents(current_plan: Optional[str]) -> Optional[str]:
    plan = (current_plan or "").strip().lower()
    return TIER_AGENT_UPGRADE.get(plan)


def tier_from_price_id(price_id: Optional[str]) -> Optional[str]:
    pid = (price_id or "").strip()
    if not pid:
        return None
    for tier, env_name in TIER_PRICE_ENV.items():
        if (os.environ.get(env_name) or "").strip() == pid:
            return tier
    return None
