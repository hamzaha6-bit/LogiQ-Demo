"""Pure picklist emit partitioning (GS-10). No Sheets API calls.

Splits exception rows, then volume-balances remaining rows across N tabs.
Primary N derivation: target_rows_per_tab. Optional tab_count override.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sheet_transforms import compute_group_boundaries

Row = Dict[str, Any]
Partition = Dict[str, Any]

_PICKLIST_TITLE_RE = re.compile(r"^(.+?)\s+(\d+)$")


class EmitError(ValueError):
    """Invalid emit-picklist params or table shape."""


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _cell(row: Row, column: str) -> str:
    return _as_str(row.get(column, ""))


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


def _coerce_positive_int(value: Any, *, label: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise EmitError(f"{label} must be a positive integer") from exc
    if n < 1:
        raise EmitError(f"{label} must be a positive integer")
    return n


def normalize_emit_table(params: Dict[str, Any]) -> Tuple[List[Row], List[str]]:
    """Accept rows/columns at top level or nested under table/data/values/output."""
    source: Any = params
    for key in ("table", "data", "values", "output"):
        nested = params.get(key)
        if isinstance(nested, dict) and ("rows" in nested or "columns" in nested):
            source = nested
            break

    rows = source.get("rows") if isinstance(source, dict) else None
    columns = source.get("columns") if isinstance(source, dict) else None
    if rows is None and isinstance(params.get("rows"), list):
        rows = params["rows"]
    if columns is None and isinstance(params.get("columns"), list):
        columns = params["columns"]

    if not isinstance(rows, list):
        raise EmitError("GS-10 requires a rows list")
    if columns is None:
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
        raise EmitError("GS-10 requires a columns list")
    columns = [str(c) for c in columns]
    normalized: List[Row] = []
    for row in rows:
        if not isinstance(row, dict):
            raise EmitError("Each row must be an object")
        normalized.append({str(k): v for k, v in row.items()})
    return normalized, columns


def is_missing_field(row: Row, field: str) -> bool:
    """Blank / whitespace / absent field → missing (exception route)."""
    if field not in row:
        return True
    return _as_str(row.get(field)) == ""


def split_exceptions(
    rows: Sequence[Row],
    exception_field: str,
) -> Tuple[List[Row], List[Row]]:
    good: List[Row] = []
    bad: List[Row] = []
    for row in rows:
        if is_missing_field(row, exception_field):
            bad.append(row)
        else:
            good.append(row)
    return good, bad


def picklist_title(index: int, *, prefix: str = "Picklist") -> str:
    """1-based title: 'Picklist 1', 'Picklist 2', …"""
    base = (prefix or "Picklist").strip() or "Picklist"
    return f"{base} {index}"


def is_managed_picklist_title(title: str, *, prefix: str = "Picklist") -> bool:
    """True for '{prefix} N' titles managed by GS-10 (idempotent cleanup)."""
    base = (prefix or "Picklist").strip() or "Picklist"
    m = _PICKLIST_TITLE_RE.match((title or "").strip())
    if not m:
        return False
    return m.group(1) == base and m.group(2).isdigit() and int(m.group(2)) >= 1


def _contiguous_groups(rows: Sequence[Row], group_column: str) -> List[List[Row]]:
    """Split into contiguous runs by group_column (blank keys are singletons)."""
    if not rows:
        return []
    boundaries = compute_group_boundaries(list(rows), group_column)
    groups: List[List[Row]] = []
    for i, start in enumerate(boundaries):
        end = boundaries[i + 1] if i + 1 < len(boundaries) else len(rows)
        groups.append(list(rows[start:end]))
    return groups


def _chunks_even(rows: Sequence[Row], tab_count: int) -> List[List[Row]]:
    """Pure even-row split into exactly tab_count partitions (may be empty)."""
    n = len(rows)
    if tab_count < 1:
        raise EmitError("tab_count must be a positive integer")
    if n == 0:
        return [[] for _ in range(tab_count)]
    base, rem = divmod(n, tab_count)
    out: List[List[Row]] = []
    idx = 0
    for i in range(tab_count):
        size = base + (1 if i < rem else 0)
        out.append(list(rows[idx : idx + size]))
        idx += size
    return out


def _pack_groups_by_target(
    groups: Sequence[Sequence[Row]],
    target_rows_per_tab: int,
) -> List[List[Row]]:
    """Greedy pack groups without splitting; start a new tab when target would be exceeded."""
    if target_rows_per_tab < 1:
        raise EmitError("target_rows_per_tab must be a positive integer")
    if not groups:
        return []
    tabs: List[List[Row]] = []
    current: List[Row] = []
    for group in groups:
        g = list(group)
        if not current:
            current = g
            continue
        if len(current) + len(g) > target_rows_per_tab:
            tabs.append(current)
            current = g
        else:
            current.extend(g)
    if current:
        tabs.append(current)
    return tabs


def _pack_groups_into_n(
    groups: Sequence[Sequence[Row]],
    tab_count: int,
) -> List[List[Row]]:
    """Distribute groups into exactly tab_count tabs (keep groups intact; may be uneven)."""
    if tab_count < 1:
        raise EmitError("tab_count must be a positive integer")
    if not groups:
        return [[] for _ in range(tab_count)]
    # Balance by cumulative row count: assign each group to the current lightest tab.
    tabs: List[List[Row]] = [[] for _ in range(tab_count)]
    weights = [0] * tab_count
    for group in groups:
        g = list(group)
        # Prefer earliest tab on ties so packing is stable / left-biased.
        i = min(range(tab_count), key=lambda k: (weights[k], k))
        tabs[i].extend(g)
        weights[i] += len(g)
    return tabs


def balance_rows(
    rows: Sequence[Row],
    *,
    target_rows_per_tab: Optional[int] = None,
    tab_count: Optional[int] = None,
    keep_groups_intact: bool = True,
    group_column: Optional[str] = None,
) -> List[List[Row]]:
    """Balance picklist rows into partitions.

    Primary: target_rows_per_tab derives N (volume-based).
    Override: tab_count forces exact partition count when provided.
    keep_groups_intact (default True): never split a contiguous group across tabs.
    """
    row_list = list(rows)
    if not row_list:
        if tab_count is not None:
            return [[] for _ in range(tab_count)]
        return []

    if tab_count is None and target_rows_per_tab is None:
        raise EmitError("GS-10 requires target_rows_per_tab (or tab_count override)")

    keep = keep_groups_intact
    if keep:
        if not (group_column or "").strip():
            raise EmitError(
                "keep_groups_intact requires group_column "
                "(or set keep_groups_intact=false for pure row balancing)"
            )
        groups = _contiguous_groups(row_list, group_column.strip())
        if tab_count is not None:
            return _pack_groups_into_n(groups, tab_count)
        assert target_rows_per_tab is not None
        return _pack_groups_by_target(groups, target_rows_per_tab)

    # Pure even-row balancing (groups may split).
    if tab_count is not None:
        n = tab_count
    else:
        assert target_rows_per_tab is not None
        n = max(1, int(math.ceil(len(row_list) / float(target_rows_per_tab))))
    return _chunks_even(row_list, n)


def build_partitions(
    rows: Sequence[Row],
    columns: Sequence[str],
    *,
    exception_field: str,
    target_rows_per_tab: Optional[int] = None,
    tab_count: Optional[int] = None,
    keep_groups_intact: bool = True,
    group_column: Optional[str] = None,
    picklist_prefix: str = "Picklist",
    exception_sheet_name: str = "Exceptions",
) -> Dict[str, Any]:
    """Split exceptions + balance remaining rows. Returns partition plan (no I/O)."""
    field = (exception_field or "").strip()
    if not field:
        raise EmitError("GS-10 requires exception_field (e.g. Lineitem sku)")

    good, bad = split_exceptions(rows, field)
    buckets = balance_rows(
        good,
        target_rows_per_tab=target_rows_per_tab,
        tab_count=tab_count,
        keep_groups_intact=keep_groups_intact,
        group_column=group_column,
    )

    picklists: List[Partition] = []
    gcol = (group_column or "").strip() or None
    for i, bucket in enumerate(buckets, start=1):
        title = picklist_title(i, prefix=picklist_prefix)
        boundaries = compute_group_boundaries(bucket, gcol) if gcol else ([0] if bucket else [])
        picklists.append(
            {
                "sheet_name": title,
                "kind": "picklist",
                "index": i,
                "rows": bucket,
                "columns": list(columns),
                "row_count": len(bucket),
                "group_boundaries": boundaries,
                "group_column": gcol,
            }
        )

    exc_name = (exception_sheet_name or "Exceptions").strip() or "Exceptions"
    exception_part: Optional[Partition] = None
    # Always include an exceptions partition descriptor when there are exceptions
    # OR when we need cleanup to clear a prior exceptions tab (caller handles empty).
    if bad:
        exception_part = {
            "sheet_name": exc_name,
            "kind": "exceptions",
            "index": 0,
            "rows": bad,
            "columns": list(columns),
            "row_count": len(bad),
            "group_boundaries": compute_group_boundaries(bad, gcol) if gcol else ([0] if bad else []),
            "group_column": gcol,
        }

    managed_titles = [p["sheet_name"] for p in picklists]
    managed_titles.append(exc_name)

    return {
        "picklists": picklists,
        "exceptions": exception_part,
        "exception_sheet_name": exc_name,
        "picklist_prefix": (picklist_prefix or "Picklist").strip() or "Picklist",
        "managed_titles": managed_titles,
        "good_row_count": len(good),
        "exception_row_count": len(bad),
        "tab_count": len(picklists),
        "keep_groups_intact": keep_groups_intact,
        "group_column": gcol,
        "target_rows_per_tab": target_rows_per_tab,
        "tab_count_override": tab_count,
        "columns": list(columns),
    }


def parse_emit_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize GS-10 params and build a partition plan."""
    rows, columns = normalize_emit_table(params)
    exception_field = (
        params.get("exception_field")
        or params.get("missing_field")
        or params.get("required_field")
        or ""
    )
    target = _coerce_positive_int(
        params.get("target_rows_per_tab")
        if "target_rows_per_tab" in params
        else params.get("rows_per_tab"),
        label="target_rows_per_tab",
    )
    tab_count = _coerce_positive_int(
        params.get("tab_count") if "tab_count" in params else params.get("n_tabs"),
        label="tab_count",
    )
    keep = _coerce_bool(
        params.get("keep_groups_intact")
        if "keep_groups_intact" in params
        else params.get("keep_groups"),
        True,
    )
    group_column = (
        params.get("group_column")
        or params.get("group_key")
        or params.get("order_column")
    )
    if group_column is not None:
        group_column = str(group_column).strip() or None

    prefix = str(params.get("picklist_prefix") or params.get("prefix") or "Picklist")
    exc_name = str(
        params.get("exception_sheet_name")
        or params.get("exceptions_sheet_name")
        or params.get("exception_tab")
        or "Exceptions"
    )

    return build_partitions(
        rows,
        columns,
        exception_field=str(exception_field),
        target_rows_per_tab=target,
        tab_count=tab_count,
        keep_groups_intact=keep,
        group_column=group_column,
        picklist_prefix=prefix,
        exception_sheet_name=exc_name,
    )
