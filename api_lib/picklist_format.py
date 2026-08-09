"""Pure GS-11 format request builders. No Sheets API calls.

GridRange notes (FLAG):
- Sheets API GridRange is 0-based, start inclusive / end exclusive.
- Header occupies row index 0; data row i (from group_boundaries) → grid row i+1.
- Boundaries must match the sheet *as written* by GS-10/GS-08. Any row insert
  between emit and format shifts indices — callers must not assume stability
  after inserts.

Print setup notes (FLAG):
- Official SheetProperties has no pageSetup / pageMargins fields.
- We do not emit fake updateSheetProperties print requests.
- Empirically, tabs created via GS-09/GS-10 DuplicateSheetRequest inherit the
  source tab's UI print/page layout; GS-11 still only applies bold/banding/
  freeze/borders and keeps print_setup_supported=false when print_setup is passed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from sheet_transforms import compute_group_boundaries

Row = Dict[str, Any]


class FormatError(ValueError):
    """Invalid GS-11 format params."""


# Default alternating band fills (light gray / white).
_DEFAULT_BAND_A = {"red": 0.93, "green": 0.93, "blue": 0.93}
_DEFAULT_BAND_B = {"red": 1.0, "green": 1.0, "blue": 1.0}

_PRINT_SETUP_FLAG = (
    "Sheets API v4 SheetProperties has no pageSetup/pageMargins fields; "
    "print margins/orientation/paper size cannot be applied via batchUpdate. "
    "Prefer inheriting print layout from a GS-09/GS-10 template duplicate; "
    "otherwise set print layout in the Sheets UI or Apps Script. "
    "This flag does not claim the API guarantees margin inheritance."
)

_GRID_INDEX_FLAG = (
    "GridRange is 0-based (start inclusive, end exclusive). "
    "group_boundaries are 0-based indices into data rows (excluding header); "
    "sheet grid row = boundary + 1. Indices are invalid after row inserts "
    "between emit and format."
)


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


def _parse_color(value: Any, default: Dict[str, float]) -> Dict[str, float]:
    if value is None:
        return dict(default)
    if isinstance(value, dict):
        out = dict(default)
        for key in ("red", "green", "blue"):
            if key in value:
                out[key] = float(value[key])
        if "alpha" in value:
            out["alpha"] = float(value["alpha"])
        return out
    text = str(value).strip().lstrip("#")
    if len(text) == 6:
        return {
            "red": int(text[0:2], 16) / 255.0,
            "green": int(text[2:4], 16) / 255.0,
            "blue": int(text[4:6], 16) / 255.0,
        }
    raise FormatError(f"Unsupported color value: {value!r}")


def _column_index(columns: Sequence[str], name: str) -> int:
    wanted = str(name).strip()
    for i, col in enumerate(columns):
        if str(col) == wanted:
            return i
    raise FormatError(f"bold column {wanted!r} not in columns: {list(columns)}")


def normalize_boundaries(
    boundaries: Optional[Sequence[Any]],
    row_count: int,
) -> List[int]:
    """Normalize group_boundaries; ensure 0 start and values within [0, row_count)."""
    if not row_count:
        return []
    if boundaries is None:
        return [0]
    out: List[int] = []
    for raw in boundaries:
        try:
            idx = int(raw)
        except (TypeError, ValueError) as exc:
            raise FormatError("group_boundaries must be integers") from exc
        if idx < 0 or idx >= row_count:
            raise FormatError(
                f"group_boundaries entry {idx} out of range for {row_count} data rows"
            )
        out.append(idx)
    out = sorted(set(out))
    if not out or out[0] != 0:
        out = [0] + [b for b in out if b != 0]
    return out


def group_ranges(
    boundaries: Sequence[int],
    row_count: int,
) -> List[Tuple[int, int]]:
    """Return (start, end) data-row half-open ranges for each group."""
    if row_count <= 0:
        return []
    bounds = list(boundaries) if boundaries else [0]
    ranges: List[Tuple[int, int]] = []
    for i, start in enumerate(bounds):
        end = bounds[i + 1] if i + 1 < len(bounds) else row_count
        if end > start:
            ranges.append((start, end))
    return ranges


def build_format_requests(
    *,
    sheet_id: int,
    columns: Sequence[str],
    row_count: int,
    bold_columns: Optional[Sequence[str]] = None,
    borders: Any = True,
    group_boundaries: Optional[Sequence[Any]] = None,
    band_colors: Optional[Sequence[Any]] = None,
    freeze_header: bool = True,
    print_setup: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build batchUpdate requests + explicit capability flags."""
    if sheet_id is None:
        raise FormatError("sheet_id is required")
    col_count = max(len(columns), 1)
    # Used grid includes header row + data rows.
    end_row = row_count + 1  # exclusive
    end_col = col_count

    flags: Dict[str, Any] = {
        "grid_range_indexing": _GRID_INDEX_FLAG,
        "print_setup_supported": False,
        "print_setup_applied": False,
    }
    requests: List[Dict[str, Any]] = []

    # Bold specified columns (header + data).
    bold_cols = [str(c).strip() for c in (bold_columns or []) if str(c).strip()]
    for name in bold_cols:
        idx = _column_index(columns, name)
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": end_row if end_row > 0 else 1,
                        "startColumnIndex": idx,
                        "endColumnIndex": idx + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"bold": True},
                        }
                    },
                    "fields": "userEnteredFormat.textFormat.bold",
                }
            }
        )

    # Always bold the header row for readability (param-driven opt-out via bold_header=false handled by caller).
    if end_row >= 1:
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": end_col,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"bold": True},
                        }
                    },
                    "fields": "userEnteredFormat.textFormat.bold",
                }
            }
        )

    apply_borders = True
    border_style = {"style": "SOLID", "width": 1, "color": {"red": 0, "green": 0, "blue": 0}}
    if isinstance(borders, dict):
        apply_borders = _coerce_bool(borders.get("enabled"), True)
        if borders.get("style"):
            border_style["style"] = str(borders["style"]).upper()
        if borders.get("width") is not None:
            border_style["width"] = int(borders["width"])
        if borders.get("color") is not None:
            border_style["color"] = _parse_color(borders["color"], border_style["color"])
    else:
        apply_borders = _coerce_bool(borders, True)

    if apply_borders and end_row > 0:
        requests.append(
            {
                "updateBorders": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": end_row,
                        "startColumnIndex": 0,
                        "endColumnIndex": end_col,
                    },
                    "top": border_style,
                    "bottom": border_style,
                    "left": border_style,
                    "right": border_style,
                    "innerHorizontal": border_style,
                    "innerVertical": border_style,
                }
            }
        )

    # Per-group alternating background (NOT naive every-other-row).
    bounds = normalize_boundaries(group_boundaries, row_count)
    colors = list(band_colors) if band_colors else [_DEFAULT_BAND_A, _DEFAULT_BAND_B]
    if len(colors) < 2:
        colors = [colors[0] if colors else _DEFAULT_BAND_A, _DEFAULT_BAND_B]
    color_a = _parse_color(colors[0], _DEFAULT_BAND_A)
    color_b = _parse_color(colors[1], _DEFAULT_BAND_B)

    for i, (start, end) in enumerate(group_ranges(bounds, row_count)):
        fill = color_a if i % 2 == 0 else color_b
        # Data rows start at grid index 1 (header at 0).
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": start + 1,
                        "endRowIndex": end + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": end_col,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": fill,
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }
        )

    if freeze_header:
        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            }
        )

    if print_setup:
        flags["print_setup_requested"] = True
        flags["print_setup_unsupported_reason"] = _PRINT_SETUP_FLAG
        # Surface requested keys without applying unsupported fields.
        flags["print_setup_requested_keys"] = sorted(
            str(k) for k in (print_setup.keys() if isinstance(print_setup, dict) else [])
        )

    flags["group_band_count"] = len(group_ranges(bounds, row_count))
    flags["bold_columns"] = bold_cols
    return requests, flags


def resolve_boundaries_from_rows(
    rows: Sequence[Row],
    group_column: str,
    provided: Optional[Sequence[Any]] = None,
) -> List[int]:
    if provided is not None:
        return normalize_boundaries(provided, len(rows))
    if not (group_column or "").strip():
        return [0] if rows else []
    return compute_group_boundaries(list(rows), group_column.strip())
