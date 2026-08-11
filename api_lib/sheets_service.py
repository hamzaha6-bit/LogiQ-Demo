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
from supabase_rest import client_id_from_user_id, rest_get, rest_patch, rest_post_with_error

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


# LLM / Blueprint templates that must never hit the Google API.
_PLACEHOLDER_SHEET_IDS = frozenset(
    {
        "your_sheet_id",
        "your_spreadsheet_id",
        "your_spreadsheet",
        "sheet_id",
        "spreadsheet_id",
        "example",
        "placeholder",
        "xxx",
        "xxxx",
        "insert_id_here",
        "paste_sheet_id_here",
    }
)
_SHEET_ID_RE = re.compile(r"^[a-zA-Z0-9-_]{20,}$")
_SHEET_URL_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
_PLACEHOLDER_HINT_RE = re.compile(
    r"(?i)\b(your[_ -]?sheet|your[_ -]?spreadsheet|example|placeholder|insert[_ -]?id|paste[_ -]?)\b"
)
_SHEET_PARAM_ERROR = (
    "Paste a real Google Sheets link in the Blueprint plan "
    "(e.g. https://docs.google.com/spreadsheets/d/<id>/edit) — "
    "placeholder values like YOUR_SHEET_ID cannot be used."
)


def is_placeholder_spreadsheet_ref(value: Optional[str]) -> bool:
    """True for empty, template, or obviously fake sheet URL/id strings."""
    raw = (value or "").strip()
    if not raw:
        return True
    if "..." in raw or raw.endswith("/d/") or "/d/…/" in raw or "/d/.../" in raw:
        return True
    lower = raw.lower()
    if lower in _PLACEHOLDER_SHEET_IDS:
        return True
    # URL with a placeholder id segment
    m = _SHEET_URL_ID_RE.search(raw)
    if m and (m.group(1).lower() in _PLACEHOLDER_SHEET_IDS or _PLACEHOLDER_HINT_RE.search(m.group(1))):
        return True
    if not m and _PLACEHOLDER_HINT_RE.search(raw) and "docs.google.com" not in lower:
        return True
    return False


def parse_spreadsheet_id(url: str) -> Optional[str]:
    """Extract spreadsheet id from a Sheets URL, or accept a raw id. Rejects placeholders."""
    raw = (url or "").strip()
    if not raw or is_placeholder_spreadsheet_ref(raw):
        return None
    match = _SHEET_URL_ID_RE.search(raw)
    if match:
        sid = match.group(1)
        if is_placeholder_spreadsheet_ref(sid):
            return None
        return sid
    # Raw spreadsheet id (no URL path)
    if _SHEET_ID_RE.match(raw) and not is_placeholder_spreadsheet_ref(raw):
        return raw
    return None


def resolve_spreadsheet_id(
    url: Optional[str] = None,
    *,
    spreadsheet_id: Optional[str] = None,
) -> str:
    """
    Resolve a real spreadsheet id from url and/or spreadsheet_id.
    Raises SheetsError with an actionable message — never call Google with placeholders.
    """
    for candidate in (spreadsheet_id, url):
        if candidate is None:
            continue
        text = str(candidate).strip()
        if not text:
            continue
        if is_placeholder_spreadsheet_ref(text):
            raise SheetsError(_SHEET_PARAM_ERROR)
        sid = parse_spreadsheet_id(text)
        if sid:
            return sid
    raise SheetsError(_SHEET_PARAM_ERROR)


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


def _a1_sheet_range(sheet_title: str, cell_range: str) -> str:
    """Build a quoted A1 range for a sheet tab (handles spaces / apostrophes)."""
    escaped = (sheet_title or "").replace("'", "''")
    return f"'{escaped}'!{cell_range}"


def _resolve_sheet_meta(
    service: Any,
    spreadsheet_id: str,
    sheet_name: Optional[str],
) -> Tuple[str, int]:
    """Return (title, sheetId). Default = first sheet; named sheet must exist (loud fail)."""
    meta = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties")
        .execute()
    )
    sheets = meta.get("sheets") or []
    if not sheets:
        raise SheetsError("Spreadsheet has no sheets")
    wanted = (sheet_name or "").strip()
    if wanted:
        for sheet in sheets:
            props = sheet.get("properties") or {}
            title = str(props.get("title") or "")
            if title == wanted:
                sheet_id = props.get("sheetId")
                if sheet_id is None:
                    raise SheetsError(f"Could not resolve sheetId for {wanted!r}")
                return title, int(sheet_id)
        available = [
            str((s.get("properties") or {}).get("title") or "")
            for s in sheets
            if (s.get("properties") or {}).get("title")
        ]
        raise SheetsError(
            f"Sheet {wanted!r} not found"
            + (f" (available: {', '.join(available)})" if available else "")
        )
    props = sheets[0].get("properties") or {}
    first = props.get("title")
    sheet_id = props.get("sheetId")
    if not first:
        raise SheetsError("Could not resolve first sheet title")
    if sheet_id is None:
        raise SheetsError("Could not resolve first sheetId")
    return str(first), int(sheet_id)


def _resolve_sheet_title(
    service: Any,
    spreadsheet_id: str,
    sheet_name: Optional[str],
) -> str:
    """Return target tab title. Default = first sheet; named sheet must exist (loud fail)."""
    title, _sheet_id = _resolve_sheet_meta(service, spreadsheet_id, sheet_name)
    return title


def _effective_sheet_name(
    conn: Optional[Dict[str, Any]],
    sheet_name: Optional[str] = None,
) -> Optional[str]:
    """Param override wins; else connection source tab; else None (= first tab)."""
    override = (sheet_name or "").strip()
    if override:
        return override
    if conn:
        stored = (conn.get("source_sheet_name") or "").strip()
        if stored:
            return stored
    return None


def _fetch_values(
    user_id: str,
    spreadsheet_id: str,
    sheet_name: Optional[str] = None,
) -> List[List[str]]:
    """Fetch A:ZZ for a tab. None/blank sheet_name → first tab (unqualified A:ZZ)."""
    service = get_sheets_service(user_id)
    wanted = (sheet_name or "").strip()
    if wanted:
        title = _resolve_sheet_title(service, spreadsheet_id, wanted)
        range_a1 = _a1_sheet_range(title, "A:ZZ")
    else:
        range_a1 = "A:ZZ"
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=range_a1)
        .execute()
    )
    return result.get("values", [])


def _qualified_range(sheet_title: Optional[str], cell_range: str) -> str:
    """Qualify A1 with tab title when known; bare range = Sheets API first-tab default."""
    wanted = (sheet_title or "").strip()
    if wanted:
        return _a1_sheet_range(wanted, cell_range)
    return cell_range


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


def _ensure_connection(
    url: str,
    agent_id: str,
    user_id: str,
    *,
    spreadsheet_id: Optional[str] = None,
    sheet_name: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Return (spreadsheet_id, conn); auto-run GS-05 connect when OAuth is valid but no lock row yet.

    Settings → Integrations shows Google Sheets "Connected" from Gmail OAuth + sheets
    scope (user_integrations), not from sheet_connections. Blueprint workflows often
    start with GS-01 + a sheet URL without a prior GS-05 step — bridge that gap here.
    """
    sid = resolve_spreadsheet_id(url, spreadsheet_id=spreadsheet_id)
    conn = get_connection(user_id, agent_id, sid)
    if conn:
        return sid, conn
    connect(url, agent_id, user_id, sheet_name=sheet_name)
    conn = get_connection(user_id, agent_id, sid)
    if not conn:
        raise SheetsError("Sheet not connected — call /api/integrations/sheets/connect first")
    return sid, conn


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


def connect(
    url: str,
    agent_id: str,
    user_id: str,
    sheet_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Connect/lock a *source* tab. Output tabs use GS-08/GS-09 with explicit sheet_name."""
    _require_sheets(user_id)
    spreadsheet_id = resolve_spreadsheet_id(url)
    # sheet_connections.client_id is NOT NULL (migration 001).
    try:
        client_id = client_id_from_user_id(user_id)
    except ValueError as exc:
        raise SheetsError(str(exc)) from exc
    agent_key = agent_id.lower().strip()
    # Resolve source tab first so missing names fail before any DB write.
    # Title resolve owns the Sheets service lookup (keeps tests patch-friendly).
    source_title = _resolve_sheet_title(
        get_sheets_service(user_id), spreadsheet_id, sheet_name
    )
    values = _fetch_values(user_id, spreadsheet_id, source_title)
    rows, columns = _rows_from_values(values)
    if not columns:
        raise SheetsError("Sheet has no header row")
    locked_schema = build_schema(columns, rows)
    row, err = rest_post_with_error(
        "sheet_connections",
        {
            "user_id": user_id,
            "client_id": client_id,
            "agent_id": agent_key,
            "spreadsheet_id": spreadsheet_id,
            "sheet_url": url.strip(),
            "source_sheet_name": source_title,
            "locked_schema": locked_schema,
            "poll_cursor": 1,
            "status": "active",
            "schema_mismatch": None,
            "locked_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="user_id,agent_id,spreadsheet_id",
    )
    if not row or not row.get("id"):
        raise SheetsError(err or "Failed to persist sheet connection")
    return {
        "success": True,
        "row_count": len(rows),
        "columns": columns,
        "spreadsheet_id": spreadsheet_id,
        "sheet_name": source_title,
        "source_sheet_name": source_title,
        "schema": locked_schema,
        "status": "active",
        "connection_id": row["id"],
    }


def read_sheet(
    url: str,
    agent_id: str,
    user_id: str,
    sheet_name: Optional[str] = None,
) -> Dict[str, Any]:
    _require_sheets(user_id)
    spreadsheet_id, conn = _ensure_connection(
        url, agent_id, user_id, sheet_name=sheet_name
    )
    if conn.get("status") == "paused_schema_mismatch":
        raise SchemaMismatchError(
            "Sheet schema changed — workflow paused",
            conn.get("schema_mismatch") or {},
        )
    target = _effective_sheet_name(conn, sheet_name)
    if target:
        service = get_sheets_service(user_id)
        title = _resolve_sheet_title(service, spreadsheet_id, target)
    else:
        title = None
    values = _fetch_values(user_id, spreadsheet_id, title)
    rows, columns = _rows_from_values(values)
    locked = conn.get("locked_schema") or {}
    diff = _validate_schema(locked, columns)
    if diff:
        _pause_connection(conn["id"], diff)
        raise SchemaMismatchError("Sheet schema changed — workflow paused", diff)
    out: Dict[str, Any] = {
        "success": True,
        "rows": rows,
        "columns": columns,
        "row_count": len(rows),
    }
    if title:
        out["sheet_name"] = title
    return out


def write_row(
    url: str,
    agent_id: str,
    user_id: str,
    row_data: Dict[str, str],
    sheet_name: Optional[str] = None,
) -> Dict[str, Any]:
    _require_sheets(user_id)
    spreadsheet_id, conn = _ensure_connection(
        url, agent_id, user_id, sheet_name=sheet_name
    )
    if conn.get("status") == "paused_schema_mismatch":
        raise SchemaMismatchError("Sheet paused due to schema mismatch", conn.get("schema_mismatch") or {})
    locked = conn.get("locked_schema") or {}
    column_names = locked.get("column_names") or [c["name"] for c in locked.get("columns", [])]
    target = _effective_sheet_name(conn, sheet_name)
    if target:
        service = get_sheets_service(user_id)
        title = _resolve_sheet_title(service, spreadsheet_id, target)
    else:
        service = get_sheets_service(user_id)
        title = None
    values = _fetch_values(user_id, spreadsheet_id, title)
    _, columns = _rows_from_values(values)
    diff = _validate_schema(locked, columns)
    if diff:
        _pause_connection(conn["id"], diff)
        raise SchemaMismatchError("Sheet schema changed — write blocked", diff)
    row_values = [str(row_data.get(col, "")) for col in column_names]
    result = (
        service.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=_qualified_range(title, "A:ZZ"),
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row_values]},
        )
        .execute()
    )
    updates = result.get("updates") or {}
    if not updates.get("updatedRange") and not updates.get("updatedRows"):
        raise SheetsError("Sheets append returned no update confirmation")
    out: Dict[str, Any] = {
        "success": True,
        "written_columns": column_names,
        "updated_range": updates.get("updatedRange"),
        "updated_rows": updates.get("updatedRows", 1),
    }
    if title:
        out["sheet_name"] = title
    return out


def update_row(
    url: str,
    agent_id: str,
    user_id: str,
    row: int,
    row_data: Dict[str, str],
    sheet_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Update a 1-based data row (row 1 = header; data starts at row 2)."""
    _require_sheets(user_id)
    spreadsheet_id = resolve_spreadsheet_id(url)
    try:
        row_num = int(row)
    except (TypeError, ValueError) as exc:
        raise SheetsError("row must be an integer") from exc
    if row_num < 2:
        raise SheetsError("row must be >= 2 (row 1 is the header)")
    spreadsheet_id, conn = _ensure_connection(
        url, agent_id, user_id, spreadsheet_id=spreadsheet_id, sheet_name=sheet_name
    )
    if conn.get("status") == "paused_schema_mismatch":
        raise SchemaMismatchError("Sheet paused due to schema mismatch", conn.get("schema_mismatch") or {})
    locked = conn.get("locked_schema") or {}
    column_names = locked.get("column_names") or [c["name"] for c in locked.get("columns", [])]
    if not column_names:
        raise SheetsError("Locked schema has no columns")
    target = _effective_sheet_name(conn, sheet_name)
    service = get_sheets_service(user_id)
    title = _resolve_sheet_title(service, spreadsheet_id, target) if target else None
    values = _fetch_values(user_id, spreadsheet_id, title)
    _, columns = _rows_from_values(values)
    diff = _validate_schema(locked, columns)
    if diff:
        _pause_connection(conn["id"], diff)
        raise SchemaMismatchError("Sheet schema changed — update blocked", diff)
    if len(values) < row_num:
        raise SheetsError(f"Row {row_num} does not exist (sheet has {len(values)} rows including header)")
    row_values = [str(row_data.get(col, "")) for col in column_names]
    end_col = _col_letter(len(column_names))
    a1 = _qualified_range(title, f"A{row_num}:{end_col}{row_num}")
    result = (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=a1,
            valueInputOption="USER_ENTERED",
            body={"values": [row_values]},
        )
        .execute()
    )
    if not result.get("updatedRange") and not result.get("updatedCells"):
        raise SheetsError("Sheets update returned no update confirmation")
    out: Dict[str, Any] = {
        "success": True,
        "row": row_num,
        "updated_range": result.get("updatedRange"),
        "updated_cells": result.get("updatedCells"),
        "written_columns": column_names,
    }
    if title:
        out["sheet_name"] = title
    return out


def write_cell(
    url: str,
    agent_id: str,
    user_id: str,
    cell: str,
    value: Any,
    sheet_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Write a single cell by A1 notation (e.g. B3)."""
    _require_sheets(user_id)
    spreadsheet_id = resolve_spreadsheet_id(url)
    a1_cell = (cell or "").strip().upper()
    if not re.match(r"^[A-Z]+\d+$", a1_cell):
        raise SheetsError("cell must be A1 notation like B3")
    # Connection optional for write_cell but preferred for schema safety / source tab.
    conn = get_connection(user_id, agent_id, spreadsheet_id)
    if conn and conn.get("status") == "paused_schema_mismatch":
        raise SchemaMismatchError("Sheet paused due to schema mismatch", conn.get("schema_mismatch") or {})
    service = get_sheets_service(user_id)
    target = _effective_sheet_name(conn, sheet_name)
    if target:
        title = _resolve_sheet_title(service, spreadsheet_id, target)
        a1 = _a1_sheet_range(title, a1_cell)
    else:
        title = None
        a1 = a1_cell
    result = (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=a1,
            valueInputOption="USER_ENTERED",
            body={"values": [["" if value is None else str(value)]]},
        )
        .execute()
    )
    if not result.get("updatedRange") and not result.get("updatedCells"):
        raise SheetsError("Sheets cell write returned no update confirmation")
    out: Dict[str, Any] = {
        "success": True,
        "cell": a1_cell,
        "value": "" if value is None else str(value),
        "updated_range": result.get("updatedRange"),
        "updated_cells": result.get("updatedCells"),
    }
    if title:
        out["sheet_name"] = title
    return out


def delete_row(
    url: str,
    agent_id: str,
    user_id: str,
    row: int,
    sheet_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Delete a 1-based sheet row (including header row 1 — caller should avoid that)."""
    _require_sheets(user_id)
    spreadsheet_id = resolve_spreadsheet_id(url)
    try:
        row_num = int(row)
    except (TypeError, ValueError) as exc:
        raise SheetsError("row must be an integer") from exc
    if row_num < 2:
        raise SheetsError("refusing to delete header row (row must be >= 2)")
    spreadsheet_id, conn = _ensure_connection(
        url, agent_id, user_id, spreadsheet_id=spreadsheet_id, sheet_name=sheet_name
    )
    service = get_sheets_service(user_id)
    target = _effective_sheet_name(conn, sheet_name)
    title, sheet_id = _resolve_sheet_meta(service, spreadsheet_id, target)
    # Sheets API DeleteDimension uses 0-based inclusive start, exclusive end.
    start = row_num - 1
    result = (
        service.spreadsheets()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "ROWS",
                                "startIndex": start,
                                "endIndex": start + 1,
                            }
                        }
                    }
                ]
            },
        )
        .execute()
    )
    replies = result.get("replies")
    if replies is None:
        raise SheetsError("Sheets delete returned no confirmation")
    return {
        "success": True,
        "row": row_num,
        "deleted": True,
        "sheet_id": sheet_id,
        "sheet_name": title,
    }


def _col_letter(n: int) -> str:
    """1-based column index to A1 letter(s)."""
    if n < 1:
        return "A"
    letters = []
    while n:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(65 + rem))
    return "".join(reversed(letters))


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off", ""):
        return False
    return default


def _normalize_bulk_columns(
    rows: List[Dict[str, Any]],
    columns: Optional[List[Any]],
) -> List[str]:
    if columns:
        out = [str(c).strip() for c in columns if str(c).strip()]
        if out:
            return out
    if not rows:
        return []
    seen: List[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row.keys():
            name = str(key).strip()
            if name and name not in seen:
                seen.append(name)
    return seen


def write_rows(
    url: str,
    user_id: str,
    rows: List[Dict[str, Any]],
    columns: Optional[List[Any]] = None,
    *,
    sheet_name: Optional[str] = None,
    clear_first: Any = False,
    spreadsheet_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Bulk-write N rows to a sheet tab in one API update.

    Intentionally bypasses schema lock / sheet_connections — for unlocked
    output tabs whose schema comes from input columns (e.g. picklist).
    Distinct from write_row (GS-02), which appends one CRM row against locked schema.
    """
    _require_sheets(user_id)
    sid = resolve_spreadsheet_id(url, spreadsheet_id=spreadsheet_id)
    if not isinstance(rows, list):
        raise SheetsError("rows must be a list of objects")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise SheetsError(f"rows[{i}] must be an object")
    column_names = _normalize_bulk_columns(rows, columns)
    if not column_names:
        raise SheetsError("columns required (or inferable from rows)")

    service = get_sheets_service(user_id)
    title = _resolve_sheet_title(service, sid, sheet_name)
    cleared = False
    if _coerce_bool(clear_first, False):
        service.spreadsheets().values().clear(
            spreadsheetId=sid,
            range=_a1_sheet_range(title, "A:ZZ"),
        ).execute()
        cleared = True

    matrix: List[List[str]] = [column_names]
    for row in rows:
        matrix.append(
            ["" if row.get(col) is None else str(row.get(col, "")) for col in column_names]
        )
    end_col = _col_letter(len(column_names))
    end_row = len(matrix)
    a1 = _a1_sheet_range(title, f"A1:{end_col}{end_row}")
    result = (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=sid,
            range=a1,
            valueInputOption="USER_ENTERED",
            body={"values": matrix},
        )
        .execute()
    )
    if not result.get("updatedRange") and not result.get("updatedCells") and not result.get("updatedRows"):
        raise SheetsError("Sheets bulk write returned no update confirmation")
    return {
        "success": True,
        "spreadsheet_id": sid,
        "sheet_name": title,
        "columns": column_names,
        "row_count": len(rows),
        "cleared": cleared,
        "updated_range": result.get("updatedRange"),
        "updated_rows": result.get("updatedRows"),
        "updated_cells": result.get("updatedCells"),
        "schema_lock": False,
    }


def _duplicate_sheet_request(
    service: Any,
    spreadsheet_id: str,
    *,
    source_sheet_id: int,
    new_sheet_name: str,
) -> Dict[str, Any]:
    """Duplicate a tab via DuplicateSheetRequest.

    Always passes newSheetName so the API does not invent 'Copy of …' orphans.
    Empirically, duplicated tabs inherit UI print/page layout from the source;
    Sheets API v4 does not expose pageSetup/pageMargins to set or verify that.
    """
    title = (new_sheet_name or "").strip()
    if not title:
        raise SheetsError("newSheetName is required when duplicating a tab")
    result = (
        service.spreadsheets()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "duplicateSheet": {
                            "sourceSheetId": int(source_sheet_id),
                            "newSheetName": title,
                        }
                    }
                ]
            },
        )
        .execute()
    )
    replies = result.get("replies") or []
    if not replies:
        raise SheetsError("Sheets duplicateSheet returned no confirmation")
    props = ((replies[0] or {}).get("duplicateSheet") or {}).get("properties") or {}
    created_title = str(props.get("title") or title)
    sheet_id = props.get("sheetId")
    if sheet_id is None:
        raise SheetsError("Sheets duplicateSheet returned no sheetId")
    return {
        "sheet_name": created_title,
        "sheet_id": int(sheet_id),
    }


def create_sheet(
    url: str,
    user_id: str,
    sheet_name: str,
    *,
    spreadsheet_id: Optional[str] = None,
    template_sheet_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new tab (GS-09).

    Modes:
    - Blank: spreadsheets.batchUpdate addSheet (default when no template).
    - Template: DuplicateSheetRequest from template_sheet_name with newSheetName.
      Missing template hard-fails — no silent fallback to addSheet.

    Output tabs are unlocked — does not write sheet_connections / schema lock.
    Missing/blank title fails loudly; duplicate title fails loudly (no silent reuse).
    """
    _require_sheets(user_id)
    sid = resolve_spreadsheet_id(url, spreadsheet_id=spreadsheet_id)
    title = (sheet_name or "").strip()
    if not title:
        raise SheetsError("sheet_name is required to create a tab")
    template = (template_sheet_name or "").strip() or None
    service = get_sheets_service(user_id)
    existing_props = _list_sheet_properties(service, sid)
    existing = [p["title"] for p in existing_props]
    by_title = {p["title"]: p["sheetId"] for p in existing_props}
    if title in existing:
        raise SheetsError(f"Sheet {title!r} already exists")

    if template:
        if template not in by_title:
            available = ", ".join(existing) if existing else "(none)"
            raise SheetsError(
                f"Template sheet {template!r} not found (available: {available})"
            )
        dup = _duplicate_sheet_request(
            service,
            sid,
            source_sheet_id=by_title[template],
            new_sheet_name=title,
        )
        return {
            "success": True,
            "spreadsheet_id": sid,
            "sheet_name": dup["sheet_name"],
            "sheet_id": dup["sheet_id"],
            "created": True,
            "created_via": "duplicate",
            "template_sheet_name": template,
            "schema_lock": False,
        }

    result = (
        service.spreadsheets()
        .batchUpdate(
            spreadsheetId=sid,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {"title": title},
                        }
                    }
                ]
            },
        )
        .execute()
    )
    replies = result.get("replies") or []
    if not replies:
        raise SheetsError("Sheets addSheet returned no confirmation")
    props = ((replies[0] or {}).get("addSheet") or {}).get("properties") or {}
    created_title = str(props.get("title") or title)
    sheet_id = props.get("sheetId")
    if sheet_id is None:
        raise SheetsError("Sheets addSheet returned no sheetId")
    return {
        "success": True,
        "spreadsheet_id": sid,
        "sheet_name": created_title,
        "sheet_id": int(sheet_id),
        "created": True,
        "created_via": "addSheet",
        "schema_lock": False,
    }


def poll(
    url: str,
    agent_id: str,
    user_id: str,
    sheet_name: Optional[str] = None,
) -> Dict[str, Any]:
    agent_key = agent_id.lower().strip()
    _require_sheets(user_id)
    spreadsheet_id, conn = _ensure_connection(
        url, agent_id, user_id, sheet_name=sheet_name
    )
    if conn.get("status") == "paused_schema_mismatch":
        raise SchemaMismatchError(
            "Sheet schema changed — poll paused",
            conn.get("schema_mismatch") or {},
        )
    target = _effective_sheet_name(conn, sheet_name)
    if target:
        service = get_sheets_service(user_id)
        title = _resolve_sheet_title(service, spreadsheet_id, target)
        values = _fetch_values(user_id, spreadsheet_id, title)
    else:
        title = None
        values = _fetch_values(user_id, spreadsheet_id, None)
    if len(values) < 2:
        out: Dict[str, Any] = {
            "success": True,
            "rows": [],
            "new_count": 0,
            "columns": [],
            "paused": False,
        }
        if title:
            out["sheet_name"] = title
        return out
    headers = [str(h).strip() for h in values[0]]
    columns = [h for h in headers if h]
    locked = conn.get("locked_schema") or {}
    diff = _validate_schema(locked, columns)
    if diff:
        _pause_connection(conn["id"], diff)
        raise SchemaMismatchError("Sheet schema changed — poll paused", diff)
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
    ok = rest_patch(
        "sheet_connections",
        {"id": conn["id"]},
        {"poll_cursor": len(values), "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    if not ok:
        raise SheetsError("Failed to update poll cursor")
    out = {
        "success": True,
        "paused": False,
        "agent": agent_key,
        "table": AGENT_TABLES.get(agent_key, "leads"),
        "rows": new_rows,
        "new_count": len(new_rows),
        "columns": columns,
        "poll_cursor": len(values),
    }
    if title:
        out["sheet_name"] = title
    return out


def connection_status(user_id: str, agent_id: str, url: str) -> Dict[str, Any]:
    try:
        spreadsheet_id = resolve_spreadsheet_id(url)
    except SheetsError:
        return {"connected": False}
    conn = get_connection(user_id, agent_id, spreadsheet_id)
    if not conn:
        return {"connected": False}
    return {
        "connected": True,
        "status": conn.get("status", "active"),
        "source_sheet_name": conn.get("source_sheet_name"),
        "schema": conn.get("locked_schema"),
        "schema_mismatch": conn.get("schema_mismatch"),
        "poll_cursor": conn.get("poll_cursor", 1),
    }


def _list_sheet_properties(service: Any, spreadsheet_id: str) -> List[Dict[str, Any]]:
    meta = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties")
        .execute()
    )
    out: List[Dict[str, Any]] = []
    for sheet in meta.get("sheets") or []:
        props = sheet.get("properties") or {}
        title = str(props.get("title") or "")
        sheet_id = props.get("sheetId")
        if not title or sheet_id is None:
            continue
        out.append({"title": title, "sheetId": int(sheet_id)})
    return out


def _delete_sheets_by_ids(
    service: Any,
    spreadsheet_id: str,
    sheet_ids: List[int],
) -> int:
    """Delete sheets by id. Refuses to delete every sheet in the spreadsheet."""
    if not sheet_ids:
        return 0
    existing = _list_sheet_properties(service, spreadsheet_id)
    if len(existing) <= len(sheet_ids):
        # Keep at least one tab — clear survivors instead of deleting the last sheet.
        keep_id = None
        for props in existing:
            if props["sheetId"] in sheet_ids:
                keep_id = props["sheetId"]
                break
        to_delete = [sid for sid in sheet_ids if sid != keep_id]
        if keep_id is not None:
            keep_title = next(
                (p["title"] for p in existing if p["sheetId"] == keep_id), None
            )
            if keep_title:
                service.spreadsheets().values().clear(
                    spreadsheetId=spreadsheet_id,
                    range=_a1_sheet_range(keep_title, "A:ZZ"),
                ).execute()
        sheet_ids = to_delete
    if not sheet_ids:
        return 0
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [{"deleteSheet": {"sheetId": sid}} for sid in sheet_ids],
        },
    ).execute()
    return len(sheet_ids)


def emit_picklist(
    url: str,
    user_id: str,
    params: Dict[str, Any],
    *,
    spreadsheet_id: Optional[str] = None,
) -> Dict[str, Any]:
    """GS-10: route exceptions + volume-balance picklist tabs (idempotent).

    Managed tab names: '{picklist_prefix} 1'..N and exception_sheet_name
    (default Exceptions). Template tabs must not use those managed names —
    reserved template names hard-fail before any managed-tab delete.

    Template mode (template_sheet_name set):
    - Always delete known managed outputs, then DuplicateSheetRequest recreate.
    - Never reuse prior blank-created tabs; never fall back to addSheet.
    - Missing template hard-fails.
    - Template / exceptions_template named like a managed output hard-fails
      (never silently delete the template).
    - Optional exceptions_template_sheet_name; else exceptions share the picklist template.

    Blank mode (no template): create missing tabs via addSheet; clear+write;
    delete only orphan managed titles not needed this run.
    """
    from picklist_emit import (  # local import avoids cycles in tests
        EmitError,
        is_managed_output_title,
        is_managed_picklist_title,
        parse_emit_params,
    )

    _require_sheets(user_id)
    sid = resolve_spreadsheet_id(url, spreadsheet_id=spreadsheet_id)

    try:
        plan = parse_emit_params(params or {})
    except EmitError as exc:
        raise SheetsError(str(exc)) from exc

    raw = params or {}
    template_name = (
        str(
            raw.get("template_sheet_name")
            if "template_sheet_name" in raw
            else raw.get("template")
            if "template" in raw
            else raw.get("from_template")
            or ""
        ).strip()
        or None
    )
    exc_template_name = (
        str(
            raw.get("exceptions_template_sheet_name")
            if "exceptions_template_sheet_name" in raw
            else raw.get("exception_template_sheet_name")
            if "exception_template_sheet_name" in raw
            else raw.get("exceptions_template")
            or ""
        ).strip()
        or None
    )
    # Shared template is fine when exceptions_template omitted.
    if template_name and not exc_template_name:
        exc_template_name = template_name
    # exceptions-only template without picklist template is unsupported — hard fail.
    if exc_template_name and not template_name:
        raise SheetsError(
            "exceptions_template_sheet_name requires template_sheet_name "
            "(or omit both for blank addSheet create)"
        )

    prefix = plan["picklist_prefix"]
    exc_name = plan["exception_sheet_name"]
    needed: List[Dict[str, Any]] = list(plan["picklists"])
    if plan["exceptions"] is not None:
        needed.append(plan["exceptions"])
    needed_titles = {p["sheet_name"] for p in needed}

    def _reject_reserved_template(name: Optional[str], *, param: str) -> None:
        """Hard-fail before any managed-tab delete if a template uses a reserved name."""
        if not name:
            return
        if is_managed_output_title(
            name, prefix=prefix, exception_sheet_name=exc_name
        ):
            raise SheetsError(
                f"{param} {name!r} matches a reserved managed output title "
                f"('{prefix} N' or {exc_name!r}). Rename the template tab so "
                "GS-10 will not delete it during managed cleanup."
            )

    # Loud fail before Sheets mutations — never silently delete a misnamed template.
    _reject_reserved_template(template_name, param="template_sheet_name")
    if exc_template_name != template_name:
        _reject_reserved_template(
            exc_template_name, param="exceptions_template_sheet_name"
        )

    service = get_sheets_service(user_id)
    existing = _list_sheet_properties(service, sid)
    by_title = {p["title"]: p["sheetId"] for p in existing}

    use_template = bool(template_name)
    if use_template:
        # Validate templates exist *before* deleting managed outputs.
        if template_name not in by_title:
            available = ", ".join(by_title) if by_title else "(none)"
            raise SheetsError(
                f"Template sheet {template_name!r} not found (available: {available})"
            )
        if exc_template_name and exc_template_name not in by_title:
            available = ", ".join(by_title) if by_title else "(none)"
            raise SheetsError(
                f"Exceptions template sheet {exc_template_name!r} not found "
                f"(available: {available})"
            )
        # Always delete + reduplicate managed outputs (idempotent recreate).
        managed_ids: List[int] = []
        for props in existing:
            title = props["title"]
            if is_managed_picklist_title(title, prefix=prefix) or title == exc_name:
                managed_ids.append(props["sheetId"])
        deleted = _delete_sheets_by_ids(service, sid, managed_ids)
    else:
        # Blank mode: remove managed titles not needed this run only.
        orphan_ids: List[int] = []
        for props in existing:
            title = props["title"]
            managed = is_managed_picklist_title(title, prefix=prefix) or title == exc_name
            if managed and title not in needed_titles:
                orphan_ids.append(props["sheetId"])
        deleted = _delete_sheets_by_ids(service, sid, orphan_ids)

    # Refresh after deletes.
    existing = _list_sheet_properties(service, sid)
    by_title = {p["title"]: p["sheetId"] for p in existing}

    written: List[Dict[str, Any]] = []
    created_titles: List[str] = []
    cleared_titles: List[str] = []
    created_via = "duplicate" if use_template else "addSheet"

    for part in needed:
        title = part["sheet_name"]
        rows = part["rows"]
        columns = part["columns"]
        if use_template:
            # Recreate every managed tab from template — never reuse / never addSheet.
            if title in by_title:
                # Should not happen after managed delete; refuse rather than reuse.
                raise SheetsError(
                    f"Managed sheet {title!r} still exists after template cleanup; "
                    "refusing to reuse (expected delete + reduplicate)"
                )
            src_title = (
                exc_template_name
                if part["kind"] == "exceptions"
                else template_name
            )
            assert src_title is not None
            if src_title not in by_title:
                available = ", ".join(by_title) if by_title else "(none)"
                raise SheetsError(
                    f"Template sheet {src_title!r} not found (available: {available})"
                )
            dup = _duplicate_sheet_request(
                service,
                sid,
                source_sheet_id=by_title[src_title],
                new_sheet_name=title,
            )
            created_titles.append(title)
            by_title[title] = int(dup["sheet_id"])
        elif title not in by_title:
            created = create_sheet(url, user_id, title, spreadsheet_id=sid)
            created_titles.append(title)
            by_title[title] = int(created["sheet_id"])
        # Always clear then write so re-runs replace prior contents / template sample rows.
        write_out = write_rows(
            url,
            user_id,
            rows,
            columns,
            sheet_name=title,
            clear_first=True,
            spreadsheet_id=sid,
        )
        cleared_titles.append(title)
        written.append(
            {
                "sheet_name": title,
                "kind": part["kind"],
                "index": part.get("index"),
                "row_count": part["row_count"],
                "group_boundaries": part.get("group_boundaries") or [],
                "group_column": part.get("group_column"),
                "sheet_id": by_title[title],
                "updated_range": write_out.get("updated_range"),
            }
        )

    return {
        "success": True,
        "spreadsheet_id": sid,
        "tabs": written,
        "picklist_tabs": [t for t in written if t["kind"] == "picklist"],
        "exception_tab": next((t for t in written if t["kind"] == "exceptions"), None),
        "tab_count": plan["tab_count"],
        "good_row_count": plan["good_row_count"],
        "exception_row_count": plan["exception_row_count"],
        "columns": plan["columns"],
        "group_column": plan["group_column"],
        "keep_groups_intact": plan["keep_groups_intact"],
        "target_rows_per_tab": plan["target_rows_per_tab"],
        "tab_count_override": plan["tab_count_override"],
        "picklist_prefix": prefix,
        "exception_sheet_name": exc_name,
        "template_sheet_name": template_name,
        "exceptions_template_sheet_name": (
            exc_template_name if use_template else None
        ),
        "created_via": created_via if needed else None,
        "deleted_orphan_tabs": deleted,
        "created_tabs": created_titles,
        "cleared_tabs": cleared_titles,
        "schema_lock": False,
        # Flatten group_boundaries for the first picklist when useful for GS-11 chaining.
        "group_boundaries": (
            (written[0].get("group_boundaries") if written else None)
            if len(written) == 1
            else None
        ),
    }


def format_picklist(
    url: str,
    user_id: str,
    params: Dict[str, Any],
    *,
    spreadsheet_id: Optional[str] = None,
) -> Dict[str, Any]:
    """GS-11: param-driven picklist formatting via spreadsheets.batchUpdate.

    Applies bold columns, borders, freeze header, and per-group alternating
    backgrounds using group_boundaries metadata (not naive every-other-row banding).

    Print/page setup is not applied here — when outputs were created via GS-09/10
    template duplicate, print layout is expected to come from the template tab.
    print_setup params remain accepted and flagged (print_setup_supported=false).

    FLAGS (also returned on the result):
    - GridRange indexing is 0-based; data boundary i → grid row i+1 (header at 0).
      Indices break if rows are inserted between emit and format.
    - Sheets API v4 has no pageSetup/pageMargins on SheetProperties; print_setup
      params are accepted and flagged as unsupported (not silently applied).
    """
    from picklist_format import (  # local import for testability
        FormatError,
        build_format_requests,
        resolve_boundaries_from_rows,
    )

    sid = resolve_spreadsheet_id(url, spreadsheet_id=spreadsheet_id)

    # Target tabs: explicit sheet_name / sheet_names, or tabs from prior GS-10.
    sheet_names: List[str] = []
    raw_names = params.get("sheet_names") or params.get("tabs")
    if isinstance(raw_names, list):
        for item in raw_names:
            if isinstance(item, dict):
                title = str(item.get("sheet_name") or item.get("title") or "").strip()
            else:
                title = str(item or "").strip()
            if title:
                sheet_names.append(title)
    single = (
        params.get("sheet_name")
        if "sheet_name" in params
        else params.get("sheet")
        if "sheet" in params
        else params.get("title")
    )
    if single and not sheet_names:
        text = str(single).strip()
        if text:
            sheet_names = [text]
    if not sheet_names:
        raise SheetsError("GS-11 requires sheet_name (or sheet_names / tabs from GS-10)")

    _require_sheets(user_id)

    bold_columns = params.get("bold_columns") or params.get("bold") or []
    if isinstance(bold_columns, str):
        bold_columns = [bold_columns]
    if not isinstance(bold_columns, list):
        raise SheetsError("bold_columns must be a list of column names")

    borders = params.get("borders") if "borders" in params else True
    band_colors = params.get("band_colors") or params.get("banding_colors")
    print_setup = params.get("print_setup") or params.get("page_setup") or params.get("print")
    freeze_header = _coerce_bool(
        params.get("freeze_header") if "freeze_header" in params else True,
        True,
    )
    group_column = (
        params.get("group_column") or params.get("group_key") or ""
    )
    provided_boundaries = params.get("group_boundaries")
    # Per-tab boundaries from GS-10 tabs metadata.
    tab_meta = {}
    if isinstance(raw_names, list):
        for item in raw_names:
            if isinstance(item, dict) and item.get("sheet_name"):
                tab_meta[str(item["sheet_name"])] = item

    service = get_sheets_service(user_id)
    formatted: List[Dict[str, Any]] = []
    all_flags: Dict[str, Any] = {}

    for title in sheet_names:
        resolved_title, sheet_id = _resolve_sheet_meta(service, sid, title)
        # Read current values to know row/column extents (and optional boundary recompute).
        values = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sid, range=_a1_sheet_range(resolved_title, "A:ZZ"))
            .execute()
            .get("values")
            or []
        )
        if values:
            columns = [str(h) for h in values[0]]
            data_rows = []
            for raw in values[1:]:
                row = {
                    columns[i]: (raw[i] if i < len(raw) else "")
                    for i in range(len(columns))
                    if columns[i]
                }
                data_rows.append(row)
        else:
            columns = list(params.get("columns") or [])
            data_rows = []

        meta = tab_meta.get(title) or {}
        boundaries = provided_boundaries
        if boundaries is None and meta.get("group_boundaries") is not None:
            boundaries = meta.get("group_boundaries")
        try:
            bounds = resolve_boundaries_from_rows(
                data_rows,
                str(group_column or meta.get("group_column") or ""),
                boundaries,
            )
            requests, flags = build_format_requests(
                sheet_id=sheet_id,
                columns=columns or ["A"],
                row_count=len(data_rows),
                bold_columns=bold_columns,
                borders=borders,
                group_boundaries=bounds,
                band_colors=band_colors,
                freeze_header=freeze_header,
                print_setup=print_setup if isinstance(print_setup, dict) else (
                    {"requested": True} if print_setup else None
                ),
            )
        except FormatError as exc:
            raise SheetsError(str(exc)) from exc

        if requests:
            result = (
                service.spreadsheets()
                .batchUpdate(spreadsheetId=sid, body={"requests": requests})
                .execute()
            )
            if result.get("replies") is None:
                raise SheetsError("Sheets format batchUpdate returned no confirmation")
        else:
            result = {"replies": []}

        all_flags = flags
        formatted.append(
            {
                "sheet_name": resolved_title,
                "sheet_id": sheet_id,
                "row_count": len(data_rows),
                "columns": columns,
                "group_boundaries": bounds,
                "request_count": len(requests),
                "requests": requests,
            }
        )

    return {
        "success": True,
        "spreadsheet_id": sid,
        "sheets": formatted,
        "sheet_name": formatted[0]["sheet_name"] if len(formatted) == 1 else None,
        "group_boundaries": formatted[0]["group_boundaries"] if len(formatted) == 1 else None,
        "request_count": sum(s["request_count"] for s in formatted),
        "flags": all_flags,
        "schema_lock": False,
    }
