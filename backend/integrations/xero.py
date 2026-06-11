import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx


def is_configured() -> bool:
    return bool(
        (os.getenv("XERO_CLIENT_ID") or "").strip()
        and (os.getenv("XERO_CLIENT_SECRET") or "").strip()
        and (os.getenv("XERO_TENANT_ID") or "").strip()
        and (
            (os.getenv("XERO_REFRESH_TOKEN") or "").strip()
            or (os.getenv("XERO_ACCESS_TOKEN") or "").strip()
        )
    )


async def _get_access_token() -> Tuple[Optional[str], str]:
    token = (os.getenv("XERO_ACCESS_TOKEN") or "").strip()
    if token:
        return token, ""

    client_id = (os.getenv("XERO_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("XERO_CLIENT_SECRET") or "").strip()
    refresh = (os.getenv("XERO_REFRESH_TOKEN") or "").strip()
    if not all([client_id, client_secret, refresh]):
        return None, "Xero not configured — set XERO_CLIENT_ID, XERO_CLIENT_SECRET, XERO_TENANT_ID and XERO_REFRESH_TOKEN"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://identity.xero.com/connect/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                },
                auth=(client_id, client_secret),
            )
            if resp.status_code != 200:
                return None, f"Xero token refresh failed: {resp.text[:200]}"
            return resp.json().get("access_token"), ""
    except Exception as exc:
        return None, str(exc)


async def fetch_overdue_invoices() -> Tuple[List[Dict[str, Any]], str]:
    token, err = await _get_access_token()
    if not token:
        return [], err

    tenant = (os.getenv("XERO_TENANT_ID") or "").strip()
    headers = {
        "Authorization": f"Bearer {token}",
        "Xero-Tenant-Id": tenant,
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                'https://api.xero.com/api.xro/2.0/Invoices?where=Status=="AUTHORISED"&order=DueDate',
                headers=headers,
            )
            if resp.status_code != 200:
                return [], f"Xero API error: {resp.text[:200]}"

            today = datetime.utcnow().date()
            invoices = []
            for inv in resp.json().get("Invoices", []):
                amount_due = float(inv.get("AmountDue", 0) or 0)
                if amount_due <= 0:
                    continue
                due_str = inv.get("DueDate", "")
                if not due_str:
                    continue
                due_date = datetime.fromisoformat(due_str.replace("Z", "")).date()
                if due_date >= today:
                    continue
                contact = inv.get("Contact", {})
                invoices.append(
                    {
                        "client": contact.get("Name", "Unknown"),
                        "amount": amount_due,
                        "currency": inv.get("CurrencyCode", "GBP"),
                        "due_date": due_str[:10],
                        "status": "overdue",
                        "emails_sent": 0,
                        "notes": f"Xero ref: {inv.get('InvoiceNumber', inv.get('InvoiceID', ''))}",
                    }
                )
            return invoices, ""
    except Exception as exc:
        return [], str(exc)
