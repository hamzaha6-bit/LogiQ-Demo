import os
from typing import Any, Dict, Tuple

import httpx


def is_configured() -> bool:
    return bool((os.getenv("HUBSPOT_API_KEY") or "").strip())


async def upsert_contact(
    name: str,
    email: str = "",
    company: str = "",
    status: str = "",
) -> Tuple[bool, str]:
    api_key = (os.getenv("HUBSPOT_API_KEY") or "").strip()
    if not api_key:
        return False, "HubSpot not configured — set HUBSPOT_API_KEY in .env"

    parts = name.split(" ", 1)
    firstname = parts[0]
    lastname = parts[1] if len(parts) > 1 else ""

    properties: Dict[str, str] = {"firstname": firstname, "lastname": lastname}
    if email:
        properties["email"] = email
    if company:
        properties["company"] = company
    if status:
        properties["hs_lead_status"] = status

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if email:
                search = await client.post(
                    "https://api.hubapi.com/crm/v3/objects/contacts/search",
                    headers=headers,
                    json={
                        "filterGroups": [
                            {
                                "filters": [
                                    {
                                        "propertyName": "email",
                                        "operator": "EQ",
                                        "value": email,
                                    }
                                ]
                            }
                        ]
                    },
                )
                if search.status_code == 200:
                    results = search.json().get("results", [])
                    if results:
                        contact_id = results[0]["id"]
                        resp = await client.patch(
                            f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}",
                            headers=headers,
                            json={"properties": properties},
                        )
                        if resp.status_code < 300:
                            return True, contact_id
                        return False, resp.text[:200]

            resp = await client.post(
                "https://api.hubapi.com/crm/v3/objects/contacts",
                headers=headers,
                json={"properties": properties},
            )
            if resp.status_code < 300:
                return True, resp.json().get("id", "created")
            return False, resp.text[:200]
    except Exception as exc:
        return False, str(exc)
