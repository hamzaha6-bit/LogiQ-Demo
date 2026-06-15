"""Google Sheets with Supabase schema lock — read, write, validate, poll."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from google_oauth import (
    SHEETS_SCOPE,
    check_gmail_health,
    get_sheets_service,
    has_scope,
    load_user_token,
)
from supabase_rest import rest_get, rest_patch, rest_post

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


class SheetsError(Exception):
    pass


class SchemaMismatchError(Exception):
    def __init__(self, message: str, diff: Dict[str, Any]):
        super().__init__(message)
        self.diff = diff


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


def _fetch_values(user_id: str, spreadsheet_id: str) -> List[List[str]]:
    service = get_sheets_service(user_id)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range="A:ZZ")
        .execute()
    )
    return result.get("values", [])


def get_connection(user_id: str, agent_id: str, spreadsheet_id: str) -> Optional[Dict[str, Any]]:
    rows = rest_get(
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
    rest_patch(
        "sheet_connections",
        {"id": conn_id},
        {
            "status": "paused_schema_mismatch",
            "schema_mismatch": diff,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _require_sheets(user_id: str) -> None:
    if not load_user_token(user_id):
        raise SheetsError("Connect Google first — /api/auth/gmail/connect")
    if not has_scope(user_id, SHEETS_SCOPE) and not has_scope(user_id, "spreadsheets.readonly"):
        raise SheetsError("Re-authorise Google for Sheets access")


def connect(url: str, agent_id: str, user_id: str) -> Dict[str, Any]:
    _require_sheets(user_id)
    spreadsheet_id = parse_spreadsheet_id(url)
    if not spreadsheet_id:
        raise SheetsError("Invalid Google Sheets URL")
    agent_key = agent_id.lower().strip()
    values = _fetch_values(user_id, spreadsheet_id)
    rows, columns = _rows_from_values(values)
    if not columns:
        raise SheetsError("Sheet has no header row")
    locked_schema = build_schema(columns, rows)
    row = rest_post(
        "sheet_connections",
        {
            "user_id": user_id,
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


def read_sheet(url: str, agent_id: str, user_id: str) -> Dict[str, Any]:
    _require_sheets(user_id)
    spreadsheet_id = parse_spreadsheet_id(url)
    if not spreadsheet_id:
        raise SheetsError("Invalid Google Sheets URL")
    conn = get_connection(user_id, agent_id, spreadsheet_id)
    if not conn:
        raise SheetsError("Sheet not connected — call /api/integrations/sheets/connect first")
    if conn.get("status") == "paused_schema_mismatch":
        raise SchemaMismatchError(
            "Sheet schema changed — workflow paused",
            conn.get("schema_mismatch") or {},
        )
    values = _fetch_values(user_id, spreadsheet_id)
    rows, columns = _rows_from_values(values)
    locked = conn.get("locked_schema") or {}
    diff = _validate_schema(locked, columns)
    if diff:
        _pause_connection(conn["id"], diff)
        raise SchemaMismatchError("Sheet schema changed — workflow paused", diff)
    return {"success": True, "rows": rows, "columns": columns, "row_count": len(rows)}


def write_row(
    url: str,
    agent_id: str,
    user_id: str,
    row_data: Dict[str, str],
) -> Dict[str, Any]:
    _require_sheets(user_id)
    spreadsheet_id = parse_spreadsheet_id(url)
    if not spreadsheet_id:
        raise SheetsError("Invalid Google Sheets URL")
    conn = get_connection(user_id, agent_id, spreadsheet_id)
    if not conn:
        raise SheetsError("Sheet not connected")
    if conn.get("status") == "paused_schema_mismatch":
        raise SchemaMismatchError("Sheet paused due to schema mismatch", conn.get("schema_mismatch") or {})
    locked = conn.get("locked_schema") or {}
    column_names = locked.get("column_names") or [c["name"] for c in locked.get("columns", [])]
    values = _fetch_values(user_id, spreadsheet_id)
    _, columns = _rows_from_values(values)
    diff = _validate_schema(locked, columns)
    if diff:
        _pause_connection(conn["id"], diff)
        raise SchemaMismatchError("Sheet schema changed — write blocked", diff)
    row_values = [str(row_data.get(col, "")) for col in column_names]
    service = get_sheets_service(user_id)
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range="A:ZZ",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row_values]},
    ).execute()
    return {"success": True, "written_columns": column_names}


def poll(url: str, agent_id: str, user_id: str) -> Dict[str, Any]:
    agent_key = agent_id.lower().strip()
    spreadsheet_id = parse_spreadsheet_id(url)
    if not spreadsheet_id:
        raise SheetsError("Invalid Google Sheets URL")
    conn = get_connection(user_id, agent_id, spreadsheet_id)
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
    rest_patch(
        "sheet_connections",
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


def connection_status(user_id: str, agent_id: str, url: str) -> Dict[str, Any]:
    spreadsheet_id = parse_spreadsheet_id(url)
    if not spreadsheet_id:
        return {"connected": False}
    conn = get_connection(user_id, agent_id, spreadsheet_id)
    if not conn:
        return {"connected": False}
    return {
        "connected": True,
        "status": conn.get("status", "active"),
        "schema": conn.get("locked_schema"),
        "schema_mismatch": conn.get("schema_mismatch"),
        "poll_cursor": conn.get("poll_cursor", 1),
    }
