"""LogiQ action primitive registry — Gmail (GM), Google Sheets (GS), Google Calendar (GC), transforms (XF)."""

from typing import Any, Dict, List, Optional

ACTION_REGISTRY: Dict[str, Dict[str, Any]] = {
    "GM-01": {"integration": "Gmail", "name": "List messages", "requires_approval": False},
    "GM-02": {"integration": "Gmail", "name": "Read message", "requires_approval": False},
    "GM-03": {"integration": "Gmail", "name": "Send email", "requires_approval": True},
    "GM-04": {"integration": "Gmail", "name": "Reply to thread", "requires_approval": True},
    "GM-05": {
        "integration": "Gmail",
        "name": "Draft email",
        "requires_approval": False,
        "params": {
            "to": "recipient email (single-draft mode)",
            "subject": "subject, or {Column} template when using rows",
            "body": "body, or {Column} template when using rows",
            "rows": (
                "optional list of row objects — creates ONE held draft per row "
                "(credit-chase fan-out). Chain from XF-06 flagged rows via "
                "{{step_N.output.rows}}. Does not send."
            ),
            "to_column": "column name for recipient email when using rows (e.g. Contact email)",
            "email_column": "alias for to_column",
        },
    },
    "GM-06": {"integration": "Gmail", "name": "Label message", "requires_approval": False},
    "GM-07": {"integration": "Gmail", "name": "Search inbox", "requires_approval": False},
    "GM-08": {"integration": "Gmail", "name": "Get thread", "requires_approval": False},
    "GS-01": {
        "integration": "Google Sheets",
        "name": "Read sheet",
        "requires_approval": False,
        "params": {
            "url": "Google Sheets URL",
            "sheet_name": "optional tab title; default connection source_sheet_name or first sheet; missing name fails loudly",
            "read_order_range": "optional; if true, also read start/end order numbers from two cells (does not change schema lock)",
            "order_range_sheet_name": "tab holding those two cells (default 'Picklist Run')",
            "start_cell": "A1 of start order number (default B1)",
            "end_cell": "A1 of end order number (default B2)",
        },
    },
    "GS-02": {
        "integration": "Google Sheets",
        "name": "Append row",
        "requires_approval": False,
        "params": {
            "url": "Google Sheets URL",
            "row": "row object",
            "sheet_name": "optional tab title; default connection source tab or first sheet",
        },
    },
    "GS-03": {
        "integration": "Google Sheets",
        "name": "Update row",
        "requires_approval": False,
        "params": {
            "url": "Google Sheets URL",
            "row": "1-based row number (>=2)",
            "row_data": "row object",
            "sheet_name": "optional tab title; default connection source tab or first sheet",
        },
    },
    "GS-04": {
        "integration": "Google Sheets",
        "name": "Poll for new rows",
        "requires_approval": False,
        "params": {
            "url": "Google Sheets URL",
            "sheet_name": "optional tab title; default connection source tab or first sheet",
        },
    },
    "GS-05": {
        "integration": "Google Sheets",
        "name": "Connect sheet",
        "requires_approval": False,
        "params": {
            "url": "Google Sheets URL",
            "sheet_name": "optional source tab to lock (stored as source_sheet_name); default first sheet",
        },
    },
    "GS-06": {
        "integration": "Google Sheets",
        "name": "Delete row",
        "requires_approval": True,
        "params": {
            "url": "Google Sheets URL",
            "row": "1-based row number (>=2)",
            "sheet_name": "optional tab title; default connection source tab or first sheet",
        },
    },
    "GS-07": {
        "integration": "Google Sheets",
        "name": "Write cell",
        "requires_approval": False,
        "params": {
            "url": "Google Sheets URL",
            "cell": "A1 notation",
            "value": "cell value",
            "sheet_name": "optional tab title; default connection source tab or first sheet",
        },
    },
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
            "sheet_name": "required tab title; missing name fails loudly (never defaults to the first sheet)",
            "clear_first": "optional; clear tab then write (default false / opt-in)",
        },
    },
    # Create output tab (Pound Fabrics picklist MVP piece 3). Not a schema-locked connection.
    "GS-09": {
        "integration": "Google Sheets",
        "name": "Create sheet tab",
        "requires_approval": False,
        "params": {
            "url": "Google Sheets URL (or spreadsheet_id)",
            "sheet_name": "required new tab title (aliases: title, name)",
            "template_sheet_name": "optional; duplicate this tab (hard-fail if missing; no addSheet fallback)",
        },
    },
    # Dedicated emit action (Pound Fabrics picklist MVP piece 4) — not generic engine branching.
    # Naming: "Picklist 1"..N + "Exceptions" (prefixes configurable).
    "GS-10": {
        "integration": "Google Sheets",
        "name": "Emit picklist tabs",
        "requires_approval": False,
        "params": {
            "url": "Google Sheets URL (or spreadsheet_id); same workbook as GS-05",
            "rows": "list of row objects (or {rows, columns} from prior XF/GS step)",
            "columns": "column name list",
            "exception_field": "required; blank/whitespace/absent → Exceptions tab (e.g. Lineitem sku)",
            "split_mode": "volume (default; target_rows_per_tab) OR sku_prefix_bands (Pound Fabrics SOP)",
            "target_rows_per_tab": "volume mode only; derives N dynamically",
            "tab_count": "volume mode optional override; exact picklist tab count when set",
            "keep_groups_intact": "volume mode; default true; do not split a group across tabs",
            "group_column": "volume mode group key (e.g. Name). Ignored for sku_prefix_bands",
            "sku_column": "sku_prefix_bands: SKU column (default Lineitem sku)",
            "product_name_column": "sku_prefix_bands: product name column (default Lineitem name)",
            "sheet1_before_product_name": "sku_prefix_bands Sheet 1 ONLY: product-name cut (default 'Plain Polycotton Fabric') — NOT a SKU prefix",
            "sku_prefix_breaks": "sku_prefix_bands sheets 2–8: 6 SKU cuts then rest, default [COT, DF, F, G, L, S]",
            "picklist_prefix": "optional; default 'Picklist' → 'Picklist 1'..N",
            "exception_sheet_name": "optional; default 'Exceptions'",
            "template_sheet_name": "optional; when set, always delete managed outputs then duplicate (no addSheet); hard-fail if name is a managed title",
            "exceptions_template_sheet_name": "optional; exceptions tab template (defaults to template_sheet_name); hard-fail if name is a managed title",
        },
    },
    # Picklist formatting via batchUpdate (Pound Fabrics picklist MVP piece 5).
    # Print/page layout: inherit via GS-09/10 template duplicate; GS-11 does not set margins.
    "GS-11": {
        "integration": "Google Sheets",
        "name": "Format picklist sheet",
        "requires_approval": False,
        "params": {
            "url": "Google Sheets URL (or spreadsheet_id)",
            "sheet_name": "target tab title (or apply to each tab from prior GS-10)",
            "bold_columns": "column name list to bold (header + data)",
            "borders": "optional bool/object; apply cell borders when true",
            "group_boundaries": "0-based data-row starts (excl. header); from XF/GS-10 metadata",
            "group_column": "optional; recompute boundaries from sheet rows when boundaries omitted",
            "band_colors": "optional [colorA, colorB] hex or {red,green,blue} for per-group banding",
            "print_setup": "accepted but not applied (print_setup_supported=false); use template tab for print layout",
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
            "id_column": "numeric ID or Shopify order Name (#694358 is 694358)",
            "min_id": "inclusive lower bound — picklist: {{step_N.output.start}} from GS-01 order-range cells",
            "max_id": "inclusive upper bound — picklist: {{step_N.output.end}} from GS-01 order-range cells",
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
            "sku_column": "first key column (e.g. Lineitem sku) — merge key is sku AND qty, never SKU alone",
            "qty_column": "second key column (e.g. Lineitem quantity)",
            "min_count": "only merge when the (sku, qty) pair appears this many times (Pound Fabrics: 4). Groups smaller than min_count stay unmerged. Default 1",
            "format_string": "summary template, e.g. \"{qty}m x {count}\"",
            "summary_column": "output column name for formatted string (default Summary)",
            "write_summary_to_qty": "optional; write 'Nm x count' into qty_column on merged rows",
        },
    },
    "XF-06": {
        "integration": "Transform",
        "name": "Derive columns then optional filter",
        "requires_approval": False,
        "params": {
            "rows": "list of row objects (from prior GS-01/XF output)",
            "columns": "column name list from prior step",
            "derive": (
                "ORDERED list of expressions evaluated left-to-right PER ROW. "
                "Later entries MAY reference earlier derived columns in the same list. "
                "Each item: {column (output name), op, ...}. "
                "Ops: gt|gte|lt|lte|eq|neq with left_column + right_column|right_value "
                "(numeric compare between columns or vs literal); "
                "days_since with date_column (today minus date → day count string); "
                "or|and with columns:[...] (truthy: yes/true/y/1). "
                "Boolean outputs default to true_value='yes' / false_value='no'. "
                "Do NOT require derived names (e.g. Flagged) to exist on the input sheet. "
                "Example credit review order: "
                "(1) Over limit = balance gt limit; "
                "(2) Days overdue = days_since oldest unpaid; "
                "(3) Overdue >30 days = Days overdue gt 30; "
                "(4) Flagged = or([Over limit, Overdue >30 days])."
            ),
            "keep_when": (
                "optional post-derive filter {column, op: equals|truthy, value}. "
                "Use column=Flagged, value=yes to keep flagged rows only."
            ),
            "filter_column": "optional alias for keep_when.column",
            "filter_value": "optional alias for keep_when.value (default yes)",
            "as_of": "optional YYYY-MM-DD for deterministic days_since (tests/demos)",
        },
    },
}

# Only these codes have real implementations in workflow_runner._execute_step.
# Phase 1 tracks add codes here as each action is verified working.
# Tracks A/B/C: all 21 Gmail, Sheets, and Calendar codes use real API calls.
# XF-01..06: pure in-memory transforms (no Sheets API).
REAL_CODES = frozenset({
    "GS-01", "GS-02", "GS-03", "GS-04", "GS-05", "GS-06", "GS-07", "GS-08", "GS-09",
    "GS-10", "GS-11",
    "GM-01", "GM-02", "GM-03", "GM-04", "GM-05", "GM-06", "GM-07", "GM-08",
    "GC-01", "GC-02", "GC-03", "GC-04", "GC-05", "GC-06",
    "XF-01", "XF-02", "XF-03", "XF-04", "XF-05", "XF-06",
})

IRREVERSIBLE_CODES = frozenset({"GM-03", "GM-04", "GC-05", "GC-06", "GS-06"})


def is_real_code(code: Optional[str]) -> bool:
    return (code or "").strip().upper() in REAL_CODES


def registry_for_prompt() -> List[Dict[str, Any]]:
    """Primitives Blueprint may plan — executable codes only."""
    out: List[Dict[str, Any]] = []
    for code, meta in ACTION_REGISTRY.items():
        if code not in REAL_CODES:
            continue
        entry: Dict[str, Any] = {
            "code": code,
            "integration": meta["integration"],
            "name": meta["name"],
            "requires_approval": meta["requires_approval"],
        }
        if isinstance(meta.get("params"), dict):
            entry["params"] = meta["params"]
        out.append(entry)
    return out


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
