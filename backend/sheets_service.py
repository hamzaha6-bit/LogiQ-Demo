"""Google Sheets with Supabase schema lock — local dev parity with api/sheets_service.py."""

from __future__ import annotations

import bootstrap_path  # noqa: F401

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from googleapiclient.discovery import build

from gmail_service import (
    GmailNotAuthorised,
    GmailNotConfigured,
    SHEETS_READONLY_SCOPE,
    SHEETS_SCOPE,
    get_credentials,
    has_sheets_scope,
    is_gmail_authorised,
    is_gmail_configured,
)
from supabase_client import get_url, is_configured, rest_headers

logger = logging.getLogger("logiq.sheets")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}|^\d{1,2}/\d{1,2}/\d{2,4}")

AGENT_TABLES = {
    "aria": "leads",
    "finn": "invoices",
    "nova": "enquiries",
    "cleo": "reports",
    "zara": "tasks",
    "vision": "leads",
}


class SheetsScopeMissing(Exception):
    pass


class SheetsError(Exception):
    pass


class SchemaMismatchError(Exception):
    def __init__(self, message: str, diff: Dict[str, Any]):
        super().__init__(message)
        self.diff = diff


def is_configured() -> bool:
    return is_gmail_configured()


def is_available() -> bool:
    return is_gmail_configured() and is_gmail_authorised() and has_sheets_scope()


def _require_access(user_id: Optional[str] = None) -> None:
    if not is_gmail_configured():
        raise GmailNotConfigured("Google not configured")
    if not is_gmail_authorised(user_id):
        raise GmailNotAuthorised("Connect Google first — visit /api/auth/gmail/connect")
    if not has_sheets_scope(user_id):
        raise SheetsScopeMissing("Re-authorise Google for Sheets access")


def parse_spreadsheet_id(url: str) -> Optional[str]:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url or "")
    return match.group(1) if match else None


def _infer_type(values: List[str]) -> str:
    cleaned = [v.strip() for v in values if v and str(v).strip()]
    if not cleaned:
        return "string"
    if all(v.lower() in ("true", "false", "yes", "no") for v in cleaned[:5]):
        return "boolean"
    if all(EMAIL_RE.match(v) for v in cleaned[:5] if "@" in v):
        return "email"
    if all(DATE_RE.match(v) for v in cleaned[:5]):
        return "date"
    try:
        [float(v.replace(",", "").replace("£", "").replace("$", "")) for v in cleaned[:5]]
        return "number"
    except ValueError:
        return "string"


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


def build_schema(columns: List[str], sample_rows: List[Dict[str, str]]) -> Dict[str, Any]:
    col_defs = []
    for idx, name in enumerate(columns):
        samples = [r.get(name, "") for r in sample_rows[:20]]
        col_defs.append(
            {
                "name": name,
                "order": idx,
                "inferred_type": _infer_type(samples),
                "sample": next((s for s in samples if s), ""),
            }
        )
    header_key = "|".join(columns)
    return {
        "version": 1,
        "columns": col_defs,
        "header_hash": hashlib.sha256(header_key.encode()).hexdigest(),
        "column_names": columns,
    }


def _rest_get(table: str, params: Dict[str, str]) -> List[Dict[str, Any]]:
    if not is_configured():
        return []
    url = f"{get_url()}/rest/v1/{table}"
    with httpx.Client(timeout=20) as client:
        resp = client.get(url, headers=rest_headers(), params=params)
        if resp.status_code >= 400:
            return []
        data = resp.json()
        return data if isinstance(data, list) else []


def _rest_post(table: str, payload: Dict[str, Any], on_conflict: str = "") -> Optional[Dict[str, Any]]:
    if not is_configured():
        return None
    url = f"{get_url()}/rest/v1/{table}"
    params = {"on_conflict": on_conflict} if on_conflict else None
    headers = {**rest_headers(), "Prefer": "resolution=merge-duplicates,return=representation"}
    with httpx.Client(timeout=20) as client:
        resp = client.post(url, headers=headers, params=params, json=payload)
        if resp.status_code >= 400:
            logger.warning("Supabase POST %s failed: %s", table, resp.text[:200])
            return None
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        return data if isinstance(data, dict) else None


def _rest_patch(match: Dict[str, str], payload: Dict[str, Any]) -> bool:
    if not is_configured():
        return False
    url = f"{get_url()}/rest/v1/sheet_connections"
    params = {k: f"eq.{v}" for k, v in match.items()}
    with httpx.Client(timeout=20) as client:
        resp = client.patch(url, headers={**rest_headers(), "Prefer": "return=minimal"}, params=params, json=payload)
        return resp.status_code < 400


def get_connection(user_id: str, agent_id: str, spreadsheet_id: str) -> Optional[Dict[str, Any]]:
    rows = _rest_get(
        "sheet_connections",
        {
            "user_id": f"eq.{user_id}",
            "agent_id": f"eq.{agent_id.lower()}",
            "spreadsheet_id": f"eq.{spreadsheet_id}",
            "select": "*",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


def _validate_schema(locked: Dict[str, Any], columns: List[str]) -> Optional[Dict[str, Any]]:
    locked_names = locked.get("column_names") or [c["name"] for c in locked.get("columns", [])]
    if locked_names == columns:
        return None
    missing = [c for c in locked_names if c not in columns]
    added = [c for c in columns if c not in locked_names]
    reordered = locked_names != columns and not missing and not added
    return {
        "missing_columns": missing,
        "added_columns": added,
        "reordered": reordered,
        "expected": locked_names,
        "actual": columns,
    }


def _pause_connection(conn_id: str, diff: Dict[str, Any]) -> None:
    _rest_patch(
        {"id": conn_id},
        {
            "status": "paused_schema_mismatch",
            "schema_mismatch": diff,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _fetch_values(user_id: Optional[str], spreadsheet_id: str) -> List[List[str]]:
    creds = get_credentials(user_id)
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    result = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range="A:ZZ").execute()
    return result.get("values", [])


def connect(url: str, agent_id: str = "aria", user_id: Optional[str] = None) -> Dict[str, Any]:
    _require_access(user_id)
    if not user_id:
        rows, columns = read_sheet_with_columns(url.strip(), user_id=user_id)
        return {
            "success": True,
            "row_count": len(rows),
            "columns": columns,
            "spreadsheet_id": parse_spreadsheet_id(url),
        }
    spreadsheet_id = parse_spreadsheet_id(url)
    if not spreadsheet_id:
        raise SheetsError("Invalid Google Sheets URL")
    # sheet_connections.client_id is NOT NULL (migration 001).
    from supabase_rest import client_id_from_user_id

    try:
        client_id = client_id_from_user_id(user_id)
    except ValueError as exc:
        raise SheetsError(str(exc)) from exc
    agent_key = agent_id.lower().strip()
    values = _fetch_values(user_id, spreadsheet_id)
    rows, columns = _rows_from_values(values)
    if not columns:
        raise SheetsError("Sheet has no header row")
    locked_schema = build_schema(columns, rows)
    row = _rest_post(
        "sheet_connections",
        {
            "user_id": user_id,
            "client_id": client_id,
            "agent_id": agent_key,
            "spreadsheet_id": spreadsheet_id,
            "sheet_url": url.strip(),
            "locked_schema": locked_schema,
            "poll_cursor": 1,
            "status": "active",
            "schema_mismatch": None,
            "locked_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="user_id,agent_id,spreadsheet_id",
    )
    return {
        "success": True,
        "row_count": len(rows),
        "columns": columns,
        "spreadsheet_id": spreadsheet_id,
        "schema": locked_schema,
        "status": "active",
        "connection_id": row.get("id") if row else None,
    }


def read_sheet_with_columns(url: str, user_id: Optional[str] = None) -> Tuple[List[Dict[str, str]], List[str]]:
    _require_access(user_id)
    spreadsheet_id = parse_spreadsheet_id(url)
    if not spreadsheet_id:
        raise SheetsError("Invalid Google Sheets URL")
    values = _fetch_values(user_id, spreadsheet_id)
    return _rows_from_values(values)


def read_sheet(url: str, user_id: Optional[str] = None) -> List[Dict[str, str]]:
    rows, _ = read_sheet_with_columns(url, user_id=user_id)
    return rows


def poll(url: str, agent: str, user_id: Optional[str] = None) -> Dict[str, Any]:
    agent_key = agent.lower().strip()
    spreadsheet_id = parse_spreadsheet_id(url)
    if not spreadsheet_id:
        raise SheetsError("Invalid Google Sheets URL")
    if not user_id:
        return _poll_legacy(url, agent_key, user_id)

    conn = get_connection(user_id, agent, spreadsheet_id)
    if not conn:
        raise SheetsError("Sheet not connected")
    if conn.get("status") == "paused_schema_mismatch":
        return {
            "success": False,
            "paused": True,
            "reason": "schema_mismatch",
            "schema_mismatch": conn.get("schema_mismatch"),
            "rows": [],
            "new_count": 0,
        }
    values = _fetch_values(user_id, spreadsheet_id)
    if len(values) < 2:
        return {"success": True, "rows": [], "new_count": 0, "columns": [], "paused": False}
    headers = [str(h).strip() for h in values[0]]
    columns = [h for h in headers if h]
    locked = conn.get("locked_schema") or {}
    diff = _validate_schema(locked, columns)
    if diff:
        _pause_connection(conn["id"], diff)
        return {
            "success": False,
            "paused": True,
            "reason": "schema_mismatch",
            "schema_mismatch": diff,
            "rows": [],
            "new_count": 0,
        }
    start_index = int(conn.get("poll_cursor") or 1)
    new_rows: List[Dict[str, str]] = []
    for raw_row in values[start_index:]:
        row: Dict[str, str] = {}
        for i, header in enumerate(headers):
            if not header:
                continue
            row[header] = raw_row[i].strip() if i < len(raw_row) else ""
        if any(v for v in row.values()):
            new_rows.append(row)
    _rest_patch(
        {"id": conn["id"]},
        {"poll_cursor": len(values), "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return {
        "success": True,
        "paused": False,
        "agent": agent_key,
        "table": AGENT_TABLES.get(agent_key, "leads"),
        "rows": new_rows,
        "new_count": len(new_rows),
        "columns": columns,
    }


_poll_state: Dict[str, int] = {}


def _poll_key(agent: str, spreadsheet_id: str) -> str:
    return f"{agent}:{spreadsheet_id}"


def _poll_legacy(url: str, agent_key: str, user_id: Optional[str]) -> Dict[str, Any]:
    _require_access(user_id)
    spreadsheet_id = parse_spreadsheet_id(url)
    if not spreadsheet_id:
        raise SheetsError("Invalid Google Sheets URL")
    values = _fetch_values(user_id, spreadsheet_id)
    if len(values) < 2:
        return {"success": True, "agent": agent_key, "table": AGENT_TABLES.get(agent_key, "leads"), "rows": [], "new_count": 0, "columns": []}
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
    return {
        "success": True,
        "agent": agent_key,
        "table": AGENT_TABLES.get(agent_key, "leads"),
        "rows": new_rows,
        "new_count": len(new_rows),
        "columns": columns,
    }


def reset_poll_state(url: str, agent: str) -> None:
    spreadsheet_id = parse_spreadsheet_id(url)
    if spreadsheet_id:
        _poll_state[_poll_key(agent.lower(), spreadsheet_id)] = 1
