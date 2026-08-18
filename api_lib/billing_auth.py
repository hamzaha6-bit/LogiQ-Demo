"""Billing mutations are owner-only. Membership is not enough."""

from __future__ import annotations

from typing import Optional

from supabase_rest import rest_get


class BillingAuthError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def require_billing_owner(user_id: Optional[str]) -> str:
    """Return client_id for the owner membership, or raise BillingAuthError."""
    uid = (user_id or "").strip()
    if not uid:
        raise BillingAuthError(401, "Not authenticated")
    rows = rest_get(
        "client_members",
        {"user_id": f"eq.{uid}", "select": "client_id,role"},
    )
    if not rows:
        raise BillingAuthError(400, f"no client membership for user {uid}")
    for row in rows:
        if (row.get("role") or "").strip().lower() != "owner":
            continue
        client_id = str(row.get("client_id") or "").strip()
        if client_id:
            return client_id
    raise BillingAuthError(403, "Only the client owner can manage billing")
