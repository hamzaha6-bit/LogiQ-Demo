"""LogiQ action primitive registry — Gmail (GM), Google Sheets (GS), Google Calendar (GC), transforms (XF)."""

from typing import Any, Dict, List, Optional

ACTION_REGISTRY: Dict[str, Dict[str, Any]] = {
    "GM-01": {"integration": "Gmail", "name": "List messages", "requires_approval": False},
    "GM-02": {"integration": "Gmail", "name": "Read message", "requires_approval": False},
    "GM-03": {"integration": "Gmail", "name": "Send email", "requires_approval": True},
    "GM-04": {"integration": "Gmail", "name": "Reply to thread", "requires_approval": True},
    "GM-05": {"integration": "Gmail", "name": "Draft email", "requires_approval": False},
    "GM-06": {"integration": "Gmail", "name": "Label message", "requires_approval": False},
    "GM-07": {"integration": "Gmail", "name": "Search inbox", "requires_approval": False},
    "GM-08": {"integration": "Gmail", "name": "Get thread", "requires_approval": False},
    "GS-01": {"integration": "Google Sheets", "name": "Read sheet", "requires_approval": False},
    "GS-02": {"integration": "Google Sheets", "name": "Append row", "requires_approval": False},
    "GS-03": {"integration": "Google Sheets", "name": "Update row", "requires_approval": False},
    "GS-04": {"integration": "Google Sheets", "name": "Poll for new rows", "requires_approval": False},
    "GS-05": {"integration": "Google Sheets", "name": "Connect sheet", "requires_approval": False},
    "GS-06": {"integration": "Google Sheets", "name": "Delete row", "requires_approval": True},
    "GS-07": {"integration": "Google Sheets", "name": "Write cell", "requires_approval": False},
    # Bulk write to unlocked output tabs (Pound Fabrics picklist MVP piece 2).
    # Distinct from GS-02 (single-row CRM append against locked schema).
    "GS-08": {
        "integration": "Google Sheets",
        "name": "Bulk write rows",
        "requires_approval": False,
        "params": {
            "url": "Google Sheets URL (or spreadsheet_id)",
            "rows": "list of row objects (or from prior XF/GS-01 output)",
            "columns": "column name list for the output schema",
            "sheet_name": "optional tab title; default first sheet; missing name fails loudly",
            "clear_first": "optional; clear tab then write (default false / opt-in)",
        },
    },
    "GC-01": {"integration": "Google Calendar", "name": "Check availability", "requires_approval": False},
    "GC-02": {"integration": "Google Calendar", "name": "List events", "requires_approval": False},
    "GC-03": {"integration": "Google Calendar", "name": "Create event", "requires_approval": False},
    "GC-04": {"integration": "Google Calendar", "name": "Update event", "requires_approval": False},
    "GC-05": {"integration": "Google Calendar", "name": "Cancel event", "requires_approval": True},
    "GC-06": {"integration": "Google Calendar", "name": "Send calendar invite", "requires_approval": True},
    # In-memory transforms (Pound Fabrics picklist MVP piece 1). Input/output: {rows, columns}.
    "XF-01": {
        "integration": "Transform",
        "name": "Filter by status and ID range",
        "requires_approval": False,
        "params": {
            "rows": "list of row objects (or from prior GS-01/XF output)",
            "columns": "column name list",
            "status_column": "status field name (e.g. Financial Status)",
            "status_value": "required equality value (e.g. paid); case-insensitive by default",
            "id_column": "numeric ID column name",
            "min_id": "inclusive lower bound",
            "max_id": "inclusive upper bound",
        },
    },
    "XF-02": {
        "integration": "Transform",
        "name": "Group-aware drop",
        "requires_approval": False,
        "params": {
            "rows": "list of row objects",
            "columns": "column name list",
            "group_column": "group key (e.g. Name / order number)",
            "match_column": "column to test (e.g. Lineitem name)",
            "op": "equals | contains (default contains; case-insensitive by default)",
            "value": "match value (e.g. Express Shipping)",
            "condition": "optional {column, op, value, case_sensitive} object",
        },
    },
    "XF-03": {
        "integration": "Transform",
        "name": "Column subset",
        "requires_approval": False,
        "params": {
            "rows": "list of row objects",
            "columns": "full column name list",
            "keep": "ordered list of column names to keep (aliases: keep_columns, select, column_list)",
        },
    },
    "XF-04": {
        "integration": "Transform",
        "name": "Sort primary then secondary",
        "requires_approval": False,
        "params": {
            "rows": "list of row objects",
            "columns": "column name list",
            "primary": "primary sort column (ascending)",
            "secondary": "secondary sort column (ascending)",
            "group_column": "optional; used for group_boundaries metadata",
        },
    },
    "XF-05": {
        "integration": "Transform",
        "name": "Aggregate by two keys",
        "requires_approval": False,
        "params": {
            "rows": "list of row objects",
            "columns": "column name list",
            "sku_column": "first key column (e.g. Lineitem sku)",
            "qty_column": "second key column (e.g. Lineitem quantity)",
            "format_string": "summary template, e.g. \"{qty}m x {count}\"",
            "summary_column": "output column name for formatted string (default Summary)",
        },
    },
}

# Only these codes have real implementations in workflow_runner._execute_step.
# Phase 1 tracks add codes here as each action is verified working.
# Tracks A/B/C: all 21 Gmail, Sheets, and Calendar codes use real API calls.
# XF-01..05: pure in-memory transforms (no Sheets API).
REAL_CODES = frozenset({
    "GS-01", "GS-02", "GS-03", "GS-04", "GS-05", "GS-06", "GS-07", "GS-08",
    "GM-01", "GM-02", "GM-03", "GM-04", "GM-05", "GM-06", "GM-07", "GM-08",
    "GC-01", "GC-02", "GC-03", "GC-04", "GC-05", "GC-06",
    "XF-01", "XF-02", "XF-03", "XF-04", "XF-05",
})

IRREVERSIBLE_CODES = frozenset({"GM-03", "GM-04", "GC-05", "GC-06", "GS-06"})


def is_real_code(code: Optional[str]) -> bool:
    return (code or "").strip().upper() in REAL_CODES


def registry_for_prompt() -> List[Dict[str, Any]]:
    """Primitives Blueprint may plan — executable codes only."""
    return [
        {
            "code": code,
            "integration": meta["integration"],
            "name": meta["name"],
            "requires_approval": meta["requires_approval"],
        }
        for code, meta in ACTION_REGISTRY.items()
        if code in REAL_CODES
    ]


def validate_plan_steps(steps: List[Dict[str, Any]]) -> Optional[str]:
    """Validate steps for persistence/execution. Only REAL_CODES are allowed."""
    if not steps:
        return "Workflow must include at least one step"
    for i, step in enumerate(steps, start=1):
        code = (step.get("code") or "").strip().upper()
        if not code:
            return f"Step {i}: missing primitive code"
        if code not in ACTION_REGISTRY:
            return f"Step {i}: unknown primitive {code!r}"
        if code not in REAL_CODES:
            return (
                f"Step {i}: action {code} is not available yet. "
                f"Only these actions work today: {', '.join(sorted(REAL_CODES))}."
            )
        step["code"] = code
        meta = ACTION_REGISTRY[code]
        if meta["requires_approval"] and not step.get("requires_approval"):
            step["requires_approval"] = True
    return None
