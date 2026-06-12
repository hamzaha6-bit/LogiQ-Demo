"""Google Sheets integration using the same OAuth token.json as Gmail."""

from __future__ import annotations

import bootstrap_path  # noqa: F401

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from googleapiclient.discovery import build

from gmail_service import (
    GmailNotAuthorised,
    GmailNotConfigured,
    get_credentials,
    has_sheets_scope,
    is_gmail_authorised,
    is_gmail_configured,
)

logger = logging.getLogger("logiq.sheets")

AGENT_TABLES: Dict[str, str] = {
    "aria": "leads",
    "finn": "invoices",
    "nova": "enquiries",
    "cleo": "reports",
    "zara": "tasks",
}

# imported row count per agent+spreadsheet (1 = header only consumed)
_poll_state: Dict[str, int] = {}


class SheetsScopeMissing(Exception):
    """token.json lacks spreadsheets.readonly — user must re-authorise."""


class SheetsError(Exception):
    """Sheets API or URL error."""


def is_configured() -> bool:
    return is_gmail_configured()


def is_available() -> bool:
    return is_gmail_configured() and is_gmail_authorised() and has_sheets_scope()


def _require_access(user_id: Optional[str] = None) -> None:
    if not is_gmail_configured():
        raise GmailNotConfigured(
            "Google not configured — set GMAIL_SENDER_EMAIL and GMAIL_CREDENTIALS_JSON in backend/.env"
        )
    if not is_gmail_authorised(user_id):
        raise GmailNotAuthorised("Connect your Gmail first — visit /api/auth/gmail/connect")
    if not has_sheets_scope(user_id):
        raise SheetsScopeMissing(
            "Re-authorise Google to add Sheets access — visit /api/auth/gmail/connect"
        )


def parse_spreadsheet_id(url: str) -> Optional[str]:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url or "")
    return match.group(1) if match else None


def _poll_key(agent: str, spreadsheet_id: str) -> str:
    return f"{agent.lower()}:{spreadsheet_id}"


def _rows_from_values(values: List[List[str]]) -> Tuple[List[Dict[str, str]], List[str]]:
    if not values:
        return [], []
    headers = [str(h).strip() for h in values[0]]
    columns = [h for h in headers if h]
    rows: List[Dict[str, str]] = []
    for raw_row in values[1:]:
        row: Dict[str, str] = {}
        for i, header in enumerate(headers):
            if not header:
                continue
            row[header] = raw_row[i].strip() if i < len(raw_row) else ""
        if any(v for v in row.values()):
            rows.append(row)
    return rows, columns


def read_sheet(url: str, user_id: Optional[str] = None) -> List[Dict[str, str]]:
    """Read a Google Sheet and return data rows as list of header→value dicts."""
    _require_access(user_id)
    spreadsheet_id = parse_spreadsheet_id(url)
    if not spreadsheet_id:
        raise SheetsError("Invalid Google Sheets URL")

    creds = get_credentials(user_id)
    try:
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range="A:ZZ")
            .execute()
        )
    except Exception as exc:
        logger.exception("Sheets API read failed for %s", spreadsheet_id)
        raise SheetsError(str(exc)) from exc

    values = result.get("values", [])
    rows, _ = _rows_from_values(values)
    logger.info("Read %d rows from sheet %s", len(rows), spreadsheet_id)
    return rows


def read_sheet_with_columns(url: str, user_id: Optional[str] = None) -> Tuple[List[Dict[str, str]], List[str]]:
    _require_access(user_id)
    spreadsheet_id = parse_spreadsheet_id(url)
    if not spreadsheet_id:
        raise SheetsError("Invalid Google Sheets URL")

    creds = get_credentials(user_id)
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range="A:ZZ")
        .execute()
    )
    values = result.get("values", [])
    return _rows_from_values(values)


def connect(url: str, user_id: Optional[str] = None) -> Dict[str, Any]:
    """Test-read a sheet URL. Returns success, row_count, and column headers."""
    rows, columns = read_sheet_with_columns(url.strip(), user_id=user_id)
    spreadsheet_id = parse_spreadsheet_id(url)
    return {
        "success": True,
        "row_count": len(rows),
        "columns": columns,
        "spreadsheet_id": spreadsheet_id,
    }


def poll(url: str, agent: str, user_id: Optional[str] = None) -> Dict[str, Any]:
    """Return new rows since last poll for this agent+sheet (raw key/value pairs)."""
    agent_key = agent.lower().strip()
    if agent_key not in AGENT_TABLES:
        raise SheetsError(f"Unknown agent {agent!r} — use aria, finn, nova, cleo, or zara")

    _require_access(user_id)
    spreadsheet_id = parse_spreadsheet_id(url)
    if not spreadsheet_id:
        raise SheetsError("Invalid Google Sheets URL")

    creds = get_credentials(user_id)
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range="A:ZZ")
        .execute()
    )
    values = result.get("values", [])
    if len(values) < 2:
        return {
            "success": True,
            "agent": agent_key,
            "table": AGENT_TABLES[agent_key],
            "rows": [],
            "new_count": 0,
            "columns": [],
        }

    headers = [str(h).strip() for h in values[0]]
    columns = [h for h in headers if h]
    state_key = _poll_key(agent_key, spreadsheet_id)
    start_index = _poll_state.get(state_key, 1)

    new_rows: List[Dict[str, str]] = []
    for raw_row in values[start_index:]:
        row: Dict[str, str] = {}
        for i, header in enumerate(headers):
            if not header:
                continue
            row[header] = raw_row[i].strip() if i < len(raw_row) else ""
        if any(v for v in row.values()):
            new_rows.append(row)

    _poll_state[state_key] = len(values)
    logger.info(
        "Sheets poll %s/%s: %d new rows (total data rows %d)",
        agent_key,
        spreadsheet_id,
        len(new_rows),
        len(values) - 1,
    )
    return {
        "success": True,
        "agent": agent_key,
        "table": AGENT_TABLES[agent_key],
        "rows": new_rows,
        "new_count": len(new_rows),
        "columns": columns,
    }


def reset_poll_state(url: str, agent: str) -> None:
    """Reset import cursor so the next poll re-imports all rows."""
    spreadsheet_id = parse_spreadsheet_id(url)
    if spreadsheet_id:
        _poll_state[_poll_key(agent.lower(), spreadsheet_id)] = 1
