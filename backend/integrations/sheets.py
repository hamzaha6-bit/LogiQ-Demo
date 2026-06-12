import bootstrap_path  # noqa: F401

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from google.oauth2 import service_account
from googleapiclient.discovery import build

SHEETS_SCOPE = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

_state: Dict[str, Any] = {
    "url": None,
    "spreadsheet_id": None,
    "imported_row_count": 1,
    "pending_leads": [],
}


def is_configured() -> bool:
    return bool((os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON") or "").strip())


def _credentials():
    raw = (os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON") or "").strip()
    if not raw:
        return None
    info = json.loads(raw)
    return service_account.Credentials.from_service_account_info(info, scopes=SHEETS_SCOPE)


def parse_spreadsheet_id(url: str) -> Optional[str]:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    return match.group(1) if match else None


def connect(url: str) -> Tuple[bool, str]:
    if not is_configured():
        return False, "Google Sheets not configured — set GOOGLE_SHEETS_CREDENTIALS_JSON in .env"
    sheet_id = parse_spreadsheet_id(url)
    if not sheet_id:
        return False, "Invalid Google Sheets URL"
    _state["url"] = url
    _state["spreadsheet_id"] = sheet_id
    _state["imported_row_count"] = 1
    _state["pending_leads"] = []
    poll()
    return True, f"Connected to sheet {sheet_id}"


def _map_row(headers: List[str], row: List[str]) -> Dict[str, str]:
    idx = {h.strip(): i for i, h in enumerate(headers)}

    def get(col: str) -> str:
        i = idx.get(col)
        return row[i].strip() if i is not None and i < len(row) else ""

    first, last = get("First Name"), get("Last Name")
    return {
        "name": " ".join(p for p in [first, last] if p),
        "company": get("Company Name") or get("Company"),
        "role": get("Title") or get("Role"),
        "email": get("Email"),
        "industry": get("Industry"),
        "status": "new",
    }


def poll() -> List[Dict[str, Any]]:
    if not _state.get("spreadsheet_id") or not is_configured():
        return []
    creds = _credentials()
    if not creds:
        return []
    try:
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=_state["spreadsheet_id"], range="A:Z")
            .execute()
        )
        values = result.get("values", [])
        if len(values) < 2:
            return []
        headers = values[0]
        start = _state["imported_row_count"]
        new_leads = []
        for row in values[start:]:
            lead = _map_row(headers, row)
            if lead.get("name") or lead.get("email") or lead.get("company"):
                new_leads.append(lead)
        _state["imported_row_count"] = len(values)
        _state["pending_leads"].extend(new_leads)
        return new_leads
    except Exception:
        return []


def get_pending_leads() -> List[Dict[str, Any]]:
    leads = list(_state["pending_leads"])
    _state["pending_leads"] = []
    return leads


def get_status() -> Dict[str, Any]:
    return {
        "configured": is_configured(),
        "connected": bool(_state.get("spreadsheet_id")),
        "url": _state.get("url"),
        "row_count": _state.get("imported_row_count", 1),
    }
