"""Pound Fabrics piece 5: GS-11 picklist formatting via batchUpdate."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "api_lib"))

from picklist_format import (  # noqa: E402
    FormatError,
    build_format_requests,
    group_ranges,
    normalize_boundaries,
)
from workflow_runner import StepExecutionError, _execute_step  # noqa: E402

SHEET_URL = "https://docs.google.com/spreadsheets/d/abc123/edit"
COLUMNS = ["Name", "Lineitem sku", "Lineitem quantity", "Summary"]


def _sheets_meta(*titles: str):
    return {
        "sheets": [
            {"properties": {"sheetId": i + 10, "title": t}} for i, t in enumerate(titles)
        ]
    }


def _values_matrix(rows):
    return [COLUMNS] + [[r.get(c, "") for c in COLUMNS] for r in rows]


# ── Pure request builders ─────────────────────────────────────────────────────

def test_bold_columns_payload():
    reqs, flags = build_format_requests(
        sheet_id=10,
        columns=COLUMNS,
        row_count=4,
        bold_columns=["Lineitem sku", "Summary"],
        borders=False,
        group_boundaries=[0],
        freeze_header=False,
    )
    bold = [r["repeatCell"] for r in reqs if "repeatCell" in r and "textFormat" in str(r)]
    # Two column bolds + header bold
    col_bolds = [
        r for r in bold
        if r["fields"] == "userEnteredFormat.textFormat.bold"
        and r["range"]["startColumnIndex"] == r["range"]["endColumnIndex"] - 1
        and r["range"]["startRowIndex"] == 0
        and r["range"]["endRowIndex"] > 1
    ]
    assert {r["range"]["startColumnIndex"] for r in col_bolds} == {1, 3}


def test_borders_payload():
    reqs, _flags = build_format_requests(
        sheet_id=10,
        columns=COLUMNS,
        row_count=3,
        bold_columns=[],
        borders=True,
        group_boundaries=[0],
        freeze_header=False,
    )
    border_reqs = [r["updateBorders"] for r in reqs if "updateBorders" in r]
    assert len(border_reqs) == 1
    br = border_reqs[0]
    assert br["range"]["endRowIndex"] == 4  # header + 3
    assert br["range"]["endColumnIndex"] == 4
    assert "innerHorizontal" in br


def test_group_banding_uses_boundaries_not_every_other_row():
    # Groups: rows 0-1, 2-4  → band only those blocks (grid rows 1-3 and 3-6)
    reqs, flags = build_format_requests(
        sheet_id=10,
        columns=COLUMNS,
        row_count=5,
        bold_columns=[],
        borders=False,
        group_boundaries=[0, 2],
        freeze_header=False,
        band_colors=["#EEEEEE", "#FFFFFF"],
    )
    band = [
        r["repeatCell"]
        for r in reqs
        if "repeatCell" in r and r["repeatCell"]["fields"] == "userEnteredFormat.backgroundColor"
    ]
    assert len(band) == 2
    assert flags["group_band_count"] == 2
    ranges = [(b["range"]["startRowIndex"], b["range"]["endRowIndex"]) for b in band]
    # data start 0 → grid 1; end 2 → grid 3; second group 2..5 → grid 3..6
    assert ranges == [(1, 3), (3, 6)]
    # Not naive every-other-row (would be 5 single-row bands)
    assert flags["group_band_count"] != 5


def test_page_setup_flagged_not_applied():
    reqs, flags = build_format_requests(
        sheet_id=10,
        columns=COLUMNS,
        row_count=2,
        bold_columns=[],
        borders=False,
        group_boundaries=[0],
        freeze_header=True,
        print_setup={"orientation": "LANDSCAPE", "margins": {"top": 0.5}},
    )
    assert flags["print_setup_supported"] is False
    assert flags["print_setup_applied"] is False
    assert flags["print_setup_requested"] is True
    assert "pageSetup" not in str(reqs)
    assert "pageMargins" not in str(reqs)
    # freeze_header still applied (supported gridProperties)
    assert any("frozenRowCount" in str(r) for r in reqs)


def test_normalize_boundaries_and_ranges():
    assert normalize_boundaries([2, 0, 2], 5) == [0, 2]
    assert group_ranges([0, 2], 5) == [(0, 2), (2, 5)]
    with pytest.raises(FormatError):
        normalize_boundaries([9], 5)


# ── Executor ──────────────────────────────────────────────────────────────────

def test_gs11_batch_update_payload(monkeypatch):
    rows = [
        {"Name": "#1", "Lineitem sku": "A", "Lineitem quantity": "1", "Summary": "x"},
        {"Name": "#1", "Lineitem sku": "B", "Lineitem quantity": "2", "Summary": "y"},
        {"Name": "#2", "Lineitem sku": "C", "Lineitem quantity": "1", "Summary": "z"},
    ]
    service = MagicMock()
    spreadsheets = service.spreadsheets.return_value
    spreadsheets.get.return_value.execute.return_value = _sheets_meta("Picklist 1")
    spreadsheets.values.return_value.get.return_value.execute.return_value = {
        "values": _values_matrix(rows)
    }
    spreadsheets.batchUpdate.return_value.execute.return_value = {
        "replies": [{}, {}, {}]
    }

    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = _execute_step(
            "GS-11",
            {
                "url": SHEET_URL,
                "sheet_name": "Picklist 1",
                "bold_columns": ["Lineitem sku"],
                "borders": True,
                "group_boundaries": [0, 2],
                "print_setup": {"orientation": "PORTRAIT", "margins": {"top": 0.75}},
            },
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )

    assert out["success"] is True
    assert out["flags"]["print_setup_supported"] is False
    assert out["flags"]["print_setup_applied"] is False
    assert "GridRange" in out["flags"]["grid_range_indexing"]
    body = spreadsheets.batchUpdate.call_args.kwargs["body"]
    reqs = body["requests"]
    assert any("updateBorders" in r for r in reqs)
    band = [
        r for r in reqs
        if r.get("repeatCell", {}).get("fields") == "userEnteredFormat.backgroundColor"
    ]
    assert len(band) == 2
    assert band[0]["repeatCell"]["range"]["startRowIndex"] == 1
    assert band[1]["repeatCell"]["range"]["startRowIndex"] == 3


def test_gs11_requires_sheet_name():
    with pytest.raises(StepExecutionError, match="sheet_name"):
        _execute_step(
            "GS-11",
            {"url": SHEET_URL, "bold_columns": ["Name"]},
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )


def test_gs11_accepts_tabs_from_gs10_metadata():
    rows = [
        {"Name": "#1", "Lineitem sku": "A", "Lineitem quantity": "1", "Summary": "x"},
        {"Name": "#2", "Lineitem sku": "B", "Lineitem quantity": "1", "Summary": "y"},
    ]
    service = MagicMock()
    spreadsheets = service.spreadsheets.return_value
    spreadsheets.get.return_value.execute.return_value = _sheets_meta("Picklist 1")
    spreadsheets.values.return_value.get.return_value.execute.return_value = {
        "values": _values_matrix(rows)
    }
    spreadsheets.batchUpdate.return_value.execute.return_value = {"replies": [{}]}

    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = _execute_step(
            "GS-11",
            {
                "url": SHEET_URL,
                "tabs": [
                    {
                        "sheet_name": "Picklist 1",
                        "group_boundaries": [0, 1],
                        "group_column": "Name",
                    }
                ],
                "bold_columns": ["Name"],
                "borders": False,
            },
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert out["group_boundaries"] == [0, 1]
    assert out["flags"]["group_band_count"] == 2
