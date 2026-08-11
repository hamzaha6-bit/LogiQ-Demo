"""Pure in-memory sheet transforms (XF-01..XF-06). No Sheets API calls.

Input / output shape matches GS-01: {"rows": [dict, ...], "columns": [str, ...]}.
Optional metadata (group_boundaries, row_count, etc.) is included for later
formatting/grouping steps (piece 5).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

Row = Dict[str, Any]
Table = Dict[str, Any]


class TransformError(ValueError):
    """Invalid transform params or table shape."""


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _cell(row: Row, column: str) -> str:
    return _as_str(row.get(column, ""))


def _require_columns(columns: Sequence[str], *needed: str) -> None:
    missing = [c for c in needed if c and c not in columns]
    if missing:
        raise TransformError(f"Missing column(s): {', '.join(missing)}")


def _normalize_table(params: Dict[str, Any]) -> Tuple[List[Row], List[str]]:
    """Accept rows/columns at top level or nested under `table` / `data`."""
    source = params
    if isinstance(params.get("table"), dict):
        source = params["table"]
    elif isinstance(params.get("data"), dict) and (
        "rows" in params["data"] or "columns" in params["data"]
    ):
        source = params["data"]

    rows = source.get("rows")
    columns = source.get("columns")
    if rows is None and isinstance(params.get("rows"), list):
        rows = params["rows"]
    if columns is None and isinstance(params.get("columns"), list):
        columns = params["columns"]

    if not isinstance(rows, list):
        raise TransformError("XF actions require a rows list")
    if columns is None:
        # Infer column order from first non-empty row keys, preserving insertion order.
        columns = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in row.keys():
                if key not in seen:
                    seen.add(key)
                    columns.append(str(key))
    if not isinstance(columns, list):
        raise TransformError("XF actions require a columns list")
    columns = [str(c) for c in columns]
    normalized_rows: List[Row] = []
    for row in rows:
        if not isinstance(row, dict):
            raise TransformError("Each row must be an object")
        normalized_rows.append({str(k): v for k, v in row.items()})
    return normalized_rows, columns


def _table_out(
    rows: List[Row],
    columns: List[str],
    *,
    group_boundaries: Optional[List[int]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Table:
    out: Table = {
        "success": True,
        "rows": rows,
        "columns": columns,
        "row_count": len(rows),
    }
    if group_boundaries is not None:
        out["group_boundaries"] = group_boundaries
    if extra:
        out.update(extra)
    return out


def compute_group_boundaries(rows: Sequence[Row], group_column: str) -> List[int]:
    """Start indices where `group_column` value changes (contiguous runs).

    Blank keys are not merged: each blank-keyed row starts its own boundary so
    piece-5 banding does not treat unrelated blanks as one order.
    """
    if not rows:
        return []
    boundaries = [0]
    prev = _cell(rows[0], group_column)
    prev_blank = prev == ""
    for i in range(1, len(rows)):
        cur = _cell(rows[i], group_column)
        cur_blank = cur == ""
        if cur_blank or prev_blank or cur != prev:
            boundaries.append(i)
        prev = cur
        prev_blank = cur_blank
    return boundaries


def _parse_number(value: Any) -> Optional[float]:
    text = _as_str(value)
    if not text:
        return None
    cleaned = text.replace(",", "").replace("£", "").replace("$", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


# ── XF-01 Filter ─────────────────────────────────────────────────────────────

def filter_rows(
    rows: List[Row],
    columns: List[str],
    *,
    status_column: str,
    status_value: str,
    id_column: str,
    min_id: Any,
    max_id: Any,
    status_case_sensitive: bool = False,
) -> Table:
    """Keep rows where status equals status_value AND id in [min_id, max_id].

    Non-numeric id cells never pass the filter.
    Status comparison is case-insensitive by default.
    """
    _require_columns(columns, status_column, id_column)
    try:
        lo = float(min_id)
        hi = float(max_id)
    except (TypeError, ValueError) as exc:
        raise TransformError("min_id and max_id must be numeric") from exc
    if lo > hi:
        raise TransformError("min_id must be <= max_id")

    target = _as_str(status_value)
    out_rows: List[Row] = []
    for row in rows:
        status = _cell(row, status_column)
        if status_case_sensitive:
            status_ok = status == target
        else:
            status_ok = status.casefold() == target.casefold()
        if not status_ok:
            continue
        num = _parse_number(row.get(id_column))
        if num is None:
            continue
        if lo <= num <= hi:
            out_rows.append(row)
    return _table_out(out_rows, list(columns))


# ── XF-02 Group-aware drop ───────────────────────────────────────────────────

def _row_matches_condition(
    row: Row,
    *,
    column: str,
    op: str,
    value: str,
    case_sensitive: bool,
) -> bool:
    cell = _cell(row, column)
    needle = _as_str(value)
    if not case_sensitive:
        cell_cmp = cell.casefold()
        needle_cmp = needle.casefold()
    else:
        cell_cmp = cell
        needle_cmp = needle
    normalized_op = (op or "equals").strip().lower()
    if normalized_op in ("equals", "eq", "=="):
        return cell_cmp == needle_cmp
    if normalized_op in ("contains", "include", "includes"):
        return needle_cmp in cell_cmp if needle_cmp else False
    raise TransformError(f"Unsupported condition op {op!r}; use equals or contains")


def drop_groups(
    rows: List[Row],
    columns: List[str],
    *,
    group_column: str,
    match_column: str,
    op: str = "contains",
    value: str = "",
    case_sensitive: bool = False,
) -> Table:
    """Drop entire groups when ANY row in the group matches the condition.

    Blank / whitespace group keys are treated as singleton groups (row-local):
    matching one blank-keyed row does not drop other blank-keyed rows.
    Default matching is case-insensitive (Shopify shipping line names vary).
    """
    _require_columns(columns, group_column, match_column)

    # Build groups preserving first-seen order. Blank keys get unique synthetic ids.
    groups: Dict[str, List[int]] = {}
    order: List[str] = []
    for idx, row in enumerate(rows):
        key = _cell(row, group_column)
        if key == "":
            group_id = f"__blank_{idx}"
        else:
            group_id = f"__key_{key}"
        if group_id not in groups:
            groups[group_id] = []
            order.append(group_id)
        groups[group_id].append(idx)

    drop_ids = set()
    for group_id, indices in groups.items():
        for idx in indices:
            if _row_matches_condition(
                rows[idx],
                column=match_column,
                op=op,
                value=value,
                case_sensitive=case_sensitive,
            ):
                drop_ids.add(group_id)
                break

    out_rows: List[Row] = []
    for group_id in order:
        if group_id in drop_ids:
            continue
        for idx in groups[group_id]:
            out_rows.append(rows[idx])

    boundaries = compute_group_boundaries(out_rows, group_column)
    return _table_out(
        out_rows,
        list(columns),
        group_boundaries=boundaries,
        extra={
            "dropped_group_count": len(drop_ids),
            "group_column": group_column,
        },
    )


# ── XF-03 Column subset ──────────────────────────────────────────────────────

def project_columns(
    rows: List[Row],
    columns: List[str],
    *,
    keep: Sequence[str],
) -> Table:
    """Project to a named list of columns (order preserved as given in keep)."""
    if not keep:
        raise TransformError("XF-03 requires a non-empty columns/keep list")
    keep_list = [str(c) for c in keep]
    _require_columns(columns, *keep_list)
    out_rows = [{c: row.get(c, "") for c in keep_list} for row in rows]
    return _table_out(out_rows, keep_list)


# ── XF-04 Sort ───────────────────────────────────────────────────────────────

def _sort_key_for(column: str) -> Callable[[Row], Tuple]:
    def key(row: Row) -> Tuple:
        raw = row.get(column, "")
        text = _as_str(raw)
        num = _parse_number(raw)
        # Numbers sort before non-numbers within the same column; both ascending.
        if num is not None:
            return (0, num, "")
        return (1, 0.0, text.casefold())

    return key


def sort_rows(
    rows: List[Row],
    columns: List[str],
    *,
    primary: str,
    secondary: str,
    group_column: Optional[str] = None,
) -> Table:
    """Sort ascending by primary, then secondary. Both required."""
    _require_columns(columns, primary, secondary)
    sorted_rows = sorted(rows, key=lambda r: (_sort_key_for(primary)(r), _sort_key_for(secondary)(r)))
    boundary_col = group_column if group_column and group_column in columns else primary
    boundaries = compute_group_boundaries(sorted_rows, boundary_col)
    return _table_out(
        sorted_rows,
        list(columns),
        group_boundaries=boundaries,
        extra={"sorted_by": [primary, secondary]},
    )


# ── XF-05 Aggregate ──────────────────────────────────────────────────────────

_FORMAT_TOKEN_RE = re.compile(r"\{(qty|count|sku|key1|key2)\}")


def aggregate_rows(
    rows: List[Row],
    columns: List[str],
    *,
    sku_column: str,
    qty_column: str,
    format_string: str,
    summary_column: str = "Summary",
    preserve_other_columns: bool = True,
) -> Table:
    """Collapse rows sharing (sku, qty) into one summary row.

    Format placeholders (configurable via format_string, not hardcoded):
      {qty}   — shared value from qty_column
      {count} — number of collapsed rows in the group
      {sku} / {key1} — shared sku_column value
      {key2}  — alias for {qty}

    Example: format_string="{qty}m x {count}" with qty=3, count=5 → "3m x 5".
    """
    _require_columns(columns, sku_column, qty_column)
    fmt = format_string if format_string is not None else ""
    if not _as_str(fmt):
        raise TransformError("XF-05 requires a non-empty format_string")

    # Preserve first-seen order of (sku, qty) pairs.
    groups: Dict[Tuple[str, str], List[Row]] = {}
    order: List[Tuple[str, str]] = []
    for row in rows:
        key = (_cell(row, sku_column), _cell(row, qty_column))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    out_columns = list(columns)
    if summary_column not in out_columns:
        out_columns.append(summary_column)

    out_rows: List[Row] = []
    group_sizes: List[int] = []
    for key in order:
        members = groups[key]
        sku_val, qty_val = key
        count = len(members)

        def repl(match: re.Match[str]) -> str:
            token = match.group(1)
            if token in ("qty", "key2"):
                return qty_val
            if token == "count":
                return str(count)
            if token in ("sku", "key1"):
                return sku_val
            return match.group(0)

        summary = _FORMAT_TOKEN_RE.sub(repl, fmt)
        if preserve_other_columns:
            base = dict(members[0])
        else:
            base = {sku_column: sku_val, qty_column: qty_val}
        base[sku_column] = sku_val
        base[qty_column] = qty_val
        base[summary_column] = summary
        # Drop columns not in out_columns when not preserving extras
        if not preserve_other_columns:
            base = {c: base.get(c, "") for c in out_columns}
        else:
            # Ensure all declared columns exist
            for c in out_columns:
                base.setdefault(c, "")
        out_rows.append(base)
        group_sizes.append(count)

    # Each aggregate row is its own group boundary (helps piece 5 if banding by summary).
    boundaries = list(range(len(out_rows)))
    return _table_out(
        out_rows,
        out_columns if preserve_other_columns else (
            [sku_column, qty_column, summary_column]
            if summary_column not in (sku_column, qty_column)
            else [sku_column, qty_column]
        ),
        group_boundaries=boundaries,
        extra={
            "group_sizes": group_sizes,
            "aggregated_from": len(rows),
            "sku_column": sku_column,
            "qty_column": qty_column,
            "summary_column": summary_column,
            "format_string": fmt,
        },
    )


# ── XF-06 Ordered derive / compute columns ───────────────────────────────────

_COMPARE_OPS = frozenset({"gt", "gte", "lt", "lte", "eq", "neq", ">", ">=", "<", "<=", "==", "!="})
_BOOL_OPS = frozenset({"or", "and"})
_DATE_OPS = frozenset({"days_since", "days_from_today", "age_days"})

_TRUTHY = frozenset({"yes", "true", "y", "1", "t"})

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%m/%d/%Y",
    "%Y/%m/%d",
    "%d %b %Y",
    "%d %B %Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
)


def _is_truthy(value: Any) -> bool:
    return _as_str(value).casefold() in _TRUTHY


def _parse_date(value: Any) -> Optional[date]:
    text = _as_str(value)
    if not text:
        return None
    # Excel serial-ish integers are uncommon in GS-01 string rows; skip.
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _normalize_compare_op(op: str) -> str:
    mapping = {
        ">": "gt",
        ">=": "gte",
        "<": "lt",
        "<=": "lte",
        "==": "eq",
        "=": "eq",
        "!=": "neq",
        "<>": "neq",
    }
    key = (op or "").strip().lower()
    return mapping.get(key, key)


def _compare_numbers(left: Optional[float], right: Optional[float], op: str) -> Optional[bool]:
    if left is None or right is None:
        return None
    op_n = _normalize_compare_op(op)
    if op_n == "gt":
        return left > right
    if op_n == "gte":
        return left >= right
    if op_n == "lt":
        return left < right
    if op_n == "lte":
        return left <= right
    if op_n == "eq":
        return left == right
    if op_n == "neq":
        return left != right
    raise TransformError(f"Unsupported compare op {op!r}")


def _resolve_side(
    row: Row,
    *,
    column: Optional[str],
    literal: Any,
    available: Sequence[str],
) -> Any:
    if column:
        if column not in available and column not in row:
            raise TransformError(f"Missing column(s): {column}")
        return row.get(column, "")
    return literal


def _bool_output(flag: Optional[bool], true_value: str, false_value: str) -> str:
    if flag is True:
        return true_value
    return false_value


def _right_side(spec: Dict[str, Any]) -> Tuple[Optional[str], Any]:
    """Return (right_column, right_literal). Prefer explicit right_column / right_value."""
    if spec.get("right_column"):
        return str(spec["right_column"]), None
    if "right_value" in spec:
        return None, spec.get("right_value")
    if "value" in spec and "right" not in spec:
        return None, spec.get("value")
    right = spec.get("right")
    if isinstance(right, str):
        # String right without right_value → column reference (Balance > Limit).
        return right, None
    if right is not None:
        return None, right
    if "value" in spec:
        return None, spec.get("value")
    return None, None


def _eval_derive_spec(
    row: Row,
    spec: Dict[str, Any],
    *,
    available: Sequence[str],
    today: date,
) -> Any:
    op_raw = _as_str(spec.get("op") or spec.get("operator")).lower()
    if not op_raw:
        raise TransformError("Each derive entry requires op")

    if "true_value" in spec:
        true_value = spec["true_value"]
    elif "yes_value" in spec:
        true_value = spec["yes_value"]
    else:
        true_value = "yes"
    if "false_value" in spec:
        false_value = spec["false_value"]
    elif "no_value" in spec:
        false_value = spec["no_value"]
    else:
        false_value = "no"
    true_value = _as_str(true_value) if true_value is not None else "yes"
    false_value = _as_str(false_value) if false_value is not None else "no"

    if op_raw in _DATE_OPS:
        date_column = (
            spec.get("date_column")
            or spec.get("column_ref")
            or spec.get("left_column")
        )
        if not date_column:
            raise TransformError("days_since requires date_column")
        parsed = _parse_date(
            _resolve_side(row, column=str(date_column), literal=None, available=available)
        )
        if parsed is None:
            return ""
        return str((today - parsed).days)

    op = _normalize_compare_op(op_raw)
    if op in {"gt", "gte", "lt", "lte", "eq", "neq"}:
        left_column = spec.get("left_column") or spec.get("left")
        left_col = str(left_column) if left_column else None
        if not left_col:
            raise TransformError(f"Compare op {op_raw!r} requires left_column")
        right_column, right_literal = _right_side(spec)
        if not right_column and right_literal is None:
            raise TransformError(
                f"Compare op {op_raw!r} requires right_column or right_value"
            )
        left_raw = _resolve_side(row, column=left_col, literal=None, available=available)
        if right_column:
            right_raw = _resolve_side(
                row, column=str(right_column), literal=None, available=available
            )
        else:
            right_raw = right_literal
        result = _compare_numbers(_parse_number(left_raw), _parse_number(right_raw), op)
        return _bool_output(result, true_value, false_value)

    if op_raw in _BOOL_OPS:
        cols = spec.get("columns") or spec.get("inputs") or spec.get("refs")
        if not isinstance(cols, list) or not cols:
            raise TransformError(f"{op_raw} requires columns: [list of column names]")
        flags = []
        for c in cols:
            val = _resolve_side(row, column=str(c), literal=None, available=available)
            flags.append(_is_truthy(val))
        if op_raw == "or":
            return _bool_output(any(flags), true_value, false_value)
        return _bool_output(all(flags), true_value, false_value)

    raise TransformError(
        f"Unsupported derive op {op_raw!r}; use gt/gte/lt/lte/eq/neq, days_since, or, and"
    )


def _collect_derive_source_columns(
    derive: Sequence[Dict[str, Any]],
    input_columns: Sequence[str],
) -> List[str]:
    """Columns that must already exist on input (not produced earlier in derive)."""
    derived: set = set()
    missing: List[str] = []
    input_set = set(input_columns)

    def need(ref: Optional[str]) -> None:
        if not ref:
            return
        name = str(ref)
        if name in derived or name in input_set:
            return
        if name not in missing:
            missing.append(name)

    for raw in derive:
        if not isinstance(raw, dict):
            raise TransformError("Each derive entry must be an object")
        out_name = raw.get("column") or raw.get("name") or raw.get("as")
        if not out_name:
            raise TransformError("Each derive entry requires column (output name)")
        op_raw = _as_str(raw.get("op") or raw.get("operator")).lower()
        op = _normalize_compare_op(op_raw)

        if op_raw in _DATE_OPS:
            need(raw.get("date_column") or raw.get("column_ref") or raw.get("left_column"))
        elif op in {"gt", "gte", "lt", "lte", "eq", "neq"}:
            need(raw.get("left_column") or raw.get("left"))
            right_column, _literal = _right_side(raw)
            if right_column:
                need(right_column)
        elif op_raw in _BOOL_OPS:
            cols = raw.get("columns") or raw.get("inputs") or raw.get("refs") or []
            if not isinstance(cols, list):
                raise TransformError(f"{op_raw} requires columns list")
            for c in cols:
                need(str(c))
        else:
            raise TransformError(
                f"Unsupported derive op {op_raw!r}; use gt/gte/lt/lte/eq/neq, days_since, or, and"
            )
        derived.add(str(out_name))

    return missing


def derive_columns(
    rows: List[Row],
    columns: List[str],
    *,
    derive: Sequence[Dict[str, Any]],
    keep_when: Optional[Dict[str, Any]] = None,
    as_of: Optional[Any] = None,
) -> Table:
    """Apply ordered column expressions; later specs may reference earlier outputs.

    Each derive entry is evaluated left-to-right **per row** so dependencies like
    Flagged = Over limit OR Overdue >30 days work in one step without Flagged
    existing on the input sheet.

    Ops:
      gt/gte/lt/lte/eq/neq — numeric compare (left_column vs right_column|right_value)
      days_since — today minus date_column (integer day count as string)
      or / and — boolean combine of columns (truthy: yes/true/y/1)

    Optional keep_when / filter_column+filter_value keeps matching rows after derive.
    """
    if not isinstance(derive, (list, tuple)) or not derive:
        raise TransformError("XF-06 requires a non-empty derive list")

    missing = _collect_derive_source_columns(derive, columns)
    if missing:
        raise TransformError(f"Missing column(s): {', '.join(missing)}")

    if as_of is None:
        today = datetime.now(timezone.utc).date()
    else:
        parsed_as_of = _parse_date(as_of)
        if parsed_as_of is None and isinstance(as_of, date):
            parsed_as_of = as_of
        if parsed_as_of is None:
            raise TransformError("as_of must be a parseable date")
        today = parsed_as_of

    out_columns = list(columns)
    derive_names: List[str] = []
    for raw in derive:
        name = str(raw.get("column") or raw.get("name") or raw.get("as"))
        derive_names.append(name)
        if name not in out_columns:
            out_columns.append(name)

    out_rows: List[Row] = []
    for row in rows:
        new_row = dict(row)
        available = list(columns)
        for spec, name in zip(derive, derive_names):
            value = _eval_derive_spec(new_row, spec, available=available, today=today)
            new_row[name] = value
            if name not in available:
                available.append(name)
        for c in out_columns:
            new_row.setdefault(c, "")
        out_rows.append(new_row)

    if isinstance(keep_when, dict) and keep_when:
        fcol = keep_when.get("column") or keep_when.get("filter_column")
        if not fcol:
            raise TransformError("keep_when requires column")
        fop = _as_str(keep_when.get("op") or "equals").lower()
        fval = keep_when.get("value")
        if fval is None:
            fval = keep_when.get("filter_value")
        fval_s = _as_str(fval)
        case_sensitive = bool(keep_when.get("case_sensitive", False))
        filtered: List[Row] = []
        for row in out_rows:
            cell = _as_str(row.get(str(fcol), ""))
            if fop in ("equals", "eq", "=="):
                if case_sensitive:
                    ok = cell == fval_s
                else:
                    ok = cell.casefold() == fval_s.casefold()
            elif fop in ("truthy", "yes", "true"):
                ok = _is_truthy(cell)
            else:
                raise TransformError(
                    f"Unsupported keep_when op {fop!r}; use equals or truthy"
                )
            if ok:
                filtered.append(row)
        out_rows = filtered

    return _table_out(
        out_rows,
        out_columns,
        extra={
            "derived_columns": derive_names,
            "as_of": today.isoformat(),
        },
    )


# ── Dispatch ─────────────────────────────────────────────────────────────────

def execute_transform(code: str, params: Dict[str, Any]) -> Table:
    """Run XF-01..XF-06 from workflow params. Raises TransformError on bad input."""
    normalized = (code or "").strip().upper()
    rows, columns = _normalize_table(params or {})

    if normalized == "XF-01":
        return filter_rows(
            rows,
            columns,
            status_column=(
                params.get("status_column")
                or params.get("status_field")
                or "Financial Status"
            ),
            status_value=params.get("status_value")
            if params.get("status_value") is not None
            else params.get("status")
            if params.get("status") is not None
            else "paid",
            id_column=params.get("id_column") or params.get("id_field") or "Id",
            min_id=params.get("min_id") if params.get("min_id") is not None else params.get("id_min"),
            max_id=params.get("max_id") if params.get("max_id") is not None else params.get("id_max"),
            status_case_sensitive=bool(params.get("status_case_sensitive", False)),
        )

    if normalized == "XF-02":
        condition = params.get("condition") if isinstance(params.get("condition"), dict) else {}
        return drop_groups(
            rows,
            columns,
            group_column=params.get("group_column") or params.get("group_key") or "Name",
            match_column=(
                condition.get("column")
                or params.get("match_column")
                or params.get("column")
                or "Lineitem name"
            ),
            op=condition.get("op") or params.get("op") or "contains",
            value=(
                condition.get("value")
                if condition.get("value") is not None
                else params.get("value")
                if params.get("value") is not None
                else "Express Shipping"
            ),
            case_sensitive=bool(
                condition.get("case_sensitive")
                if "case_sensitive" in condition
                else params.get("case_sensitive", False)
            ),
        )

    if normalized == "XF-03":
        keep = (
            params.get("keep")
            or params.get("keep_columns")
            or params.get("select")
            or params.get("column_list")
        )
        # Avoid treating the table's full `columns` as the projection list when
        # the caller forgot keep/select — require an explicit projection list.
        if keep is None and isinstance(params.get("project"), list):
            keep = params["project"]
        if not isinstance(keep, list):
            raise TransformError(
                "XF-03 requires keep / keep_columns / select / column_list (list of column names)"
            )
        return project_columns(rows, columns, keep=keep)

    if normalized == "XF-04":
        primary = params.get("primary") or params.get("primary_column") or params.get("sort_by")
        secondary = (
            params.get("secondary")
            or params.get("secondary_column")
            or params.get("then_by")
        )
        if not primary or not secondary:
            raise TransformError("XF-04 requires primary and secondary column names")
        return sort_rows(
            rows,
            columns,
            primary=str(primary),
            secondary=str(secondary),
            group_column=params.get("group_column"),
        )

    if normalized == "XF-05":
        sku_column = (
            params.get("sku_column")
            or params.get("key1_column")
            or params.get("key_column")
            or "Lineitem sku"
        )
        qty_column = (
            params.get("qty_column")
            or params.get("key2_column")
            or params.get("quantity_column")
            or "Lineitem quantity"
        )
        format_string = (
            params.get("format_string")
            or params.get("format")
            or params.get("summary_format")
        )
        if format_string is None:
            raise TransformError("XF-05 requires format_string (e.g. \"{qty}m x {count}\")")
        return aggregate_rows(
            rows,
            columns,
            sku_column=str(sku_column),
            qty_column=str(qty_column),
            format_string=str(format_string),
            summary_column=str(params.get("summary_column") or "Summary"),
            preserve_other_columns=bool(params.get("preserve_other_columns", True)),
        )

    if normalized == "XF-06":
        derive = (
            params.get("derive")
            or params.get("derived_columns")
            or params.get("computations")
            or params.get("expressions")
        )
        if not isinstance(derive, list):
            raise TransformError(
                "XF-06 requires derive (ordered list of {column, op, ...} expressions)"
            )
        keep_when = params.get("keep_when") if isinstance(params.get("keep_when"), dict) else None
        if keep_when is None and params.get("filter_column"):
            keep_when = {
                "column": params.get("filter_column"),
                "op": params.get("filter_op") or "equals",
                "value": (
                    params.get("filter_value")
                    if params.get("filter_value") is not None
                    else "yes"
                ),
            }
        return derive_columns(
            rows,
            columns,
            derive=derive,
            keep_when=keep_when,
            as_of=params.get("as_of") or params.get("today"),
        )

    raise TransformError(f"Unhandled transform action {normalized}")
