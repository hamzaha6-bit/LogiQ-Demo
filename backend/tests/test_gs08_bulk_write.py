"""Pound Fabrics piece 2: GS-08 bulk write (mocked Sheets API)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "api_lib"))

from sheets_service import write_row, write_rows  # noqa: E402
from workflow_runner import StepExecutionError, _execute_step  # noqa: E402

SHEET_URL = "https://docs.google.com/spreadsheets/d/abc123/edit"
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pound_fabrics" / "shopify_orders.json"


@pytest.fixture
def picklist_table():
    """Subset shaped like XF output for picklist write."""
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    columns = ["Name", "Lineitem sku", "Lineitem quantity", "Lineitem name"]
    rows = [{c: r.get(c, "") for c in columns} for r in data["rows"][:5]]
    return {"rows": rows, "columns": columns}


def _sheets_meta(*titles: str):
    return {
        "sheets": [
            {"properties": {"sheetId": i, "title": t}} for i, t in enumerate(titles)
        ]
    }


def _mock_service(*, titles=("Sheet1", "Picklist"), update_result=None, clear_result=None):
    service = MagicMock()
    spreadsheets = service.spreadsheets.return_value
    spreadsheets.get.return_value.execute.return_value = _sheets_meta(*titles)
    values_api = spreadsheets.values.return_value
    values_api.update.return_value.execute.return_value = update_result or {
        "updatedRange": "'Picklist'!A1:D6",
        "updatedRows": 6,
        "updatedCells": 24,
    }
    values_api.clear.return_value.execute.return_value = clear_result or {
        "clearedRange": "'Picklist'!A:ZZ"
    }
    return service, values_api


# ── Bulk write N rows ─────────────────────────────────────────────────────────

def test_gs08_bulk_writes_n_rows_in_one_update(picklist_table):
    service, values_api = _mock_service(titles=("Sheet1", "Picklist"))
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=service), \
         patch("sheets_service.get_connection") as mock_conn:
        out = _execute_step(
            "GS-08",
            {
                "url": SHEET_URL,
                "sheet_name": "Picklist",
                **picklist_table,
            },
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )

    assert out["success"] is True
    assert out["row_count"] == 5
    assert out["sheet_name"] == "Picklist"
    assert out["columns"] == picklist_table["columns"]
    assert out["schema_lock"] is False
    assert out["cleared"] is False
    mock_conn.assert_not_called()

    values_api.update.assert_called_once()
    kwargs = values_api.update.call_args.kwargs
    assert kwargs["spreadsheetId"] == "abc123"
    assert kwargs["valueInputOption"] == "RAW"
    assert kwargs["range"].startswith("'Picklist'!A1:")
    body_values = kwargs["body"]["values"]
    assert body_values[0] == picklist_table["columns"]
    assert len(body_values) == 6  # header + 5 rows
    values_api.clear.assert_not_called()


def test_gs08_accepts_nested_transform_shaped_data(picklist_table):
    service, _values_api = _mock_service()
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = _execute_step(
            "GS-08",
            {"url": SHEET_URL, "data": picklist_table, "sheet_name": "Picklist"},
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert out["row_count"] == 5
    assert out["success"] is True


# ── Opt-in clear then write ───────────────────────────────────────────────────

def test_gs08_clear_first_defaults_false(picklist_table):
    service, values_api = _mock_service()
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = write_rows(
            SHEET_URL,
            "u1",
            picklist_table["rows"],
            picklist_table["columns"],
            sheet_name="Picklist",
        )
    assert out["cleared"] is False
    values_api.clear.assert_not_called()


def test_gs08_opt_in_clear_then_write(picklist_table):
    service, values_api = _mock_service()
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = _execute_step(
            "GS-08",
            {
                "url": SHEET_URL,
                "sheet_name": "Picklist",
                "clear_first": True,
                **picklist_table,
            },
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert out["cleared"] is True
    values_api.clear.assert_called_once()
    clear_kwargs = values_api.clear.call_args.kwargs
    assert clear_kwargs["spreadsheetId"] == "abc123"
    assert clear_kwargs["range"] == "'Picklist'!A:ZZ"
    values_api.update.assert_called_once()


# ── Schema lock bypass ───────────────────────────────────────────────────────

def test_gs08_does_not_require_or_use_schema_lock(picklist_table):
    """Output schema comes from params — no sheet_connections / locked_schema."""
    service, values_api = _mock_service()
    custom_columns = ["Sku", "Qty", "Summary"]
    custom_rows = [
        {"Sku": "FAB-1", "Qty": "2", "Summary": "2m x 1"},
        {"Sku": "FAB-2", "Qty": "5", "Summary": "5m x 3"},
    ]
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=service), \
         patch("sheets_service.get_connection") as mock_conn, \
         patch("sheets_service._validate_schema") as mock_validate:
        out = write_rows(
            SHEET_URL,
            "u1",
            custom_rows,
            custom_columns,
            sheet_name="Picklist",
        )

    assert out["schema_lock"] is False
    assert out["columns"] == custom_columns
    mock_conn.assert_not_called()
    mock_validate.assert_not_called()
    body_values = values_api.update.call_args.kwargs["body"]["values"]
    assert body_values[0] == custom_columns
    assert body_values[1] == ["FAB-1", "2", "2m x 1"]


def test_gs02_still_uses_schema_lock():
    """Regression: GS-02 remains single-row CRM with locked schema."""
    conn = {
        "id": "conn-1",
        "status": "active",
        "locked_schema": {
            "column_names": ["Name", "Email"],
            "columns": [{"name": "Name"}, {"name": "Email"}],
        },
        "poll_cursor": 1,
        "schema_mismatch": None,
    }
    service = MagicMock()
    service.spreadsheets().values().append().execute.return_value = {
        "updates": {"updatedRange": "Sheet1!A2:B2", "updatedRows": 1}
    }
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_connection", return_value=conn) as mock_conn, \
         patch("sheets_service._fetch_values", return_value=[["Name", "Email"]]), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = write_row(
            SHEET_URL,
            "aria",
            "u1",
            {"Name": "Ada", "Email": "a@x.com", "ExtraIgnored": "x"},
        )
    assert out["success"] is True
    assert out["written_columns"] == ["Name", "Email"]
    mock_conn.assert_called()
    append_body = service.spreadsheets().values().append.call_args.kwargs["body"]["values"]
    assert append_body == [["Ada", "a@x.com"]]


# ── sheet_name targeting ─────────────────────────────────────────────────────

def test_gs08_defaults_to_first_sheet_when_name_omitted(picklist_table):
    service, values_api = _mock_service(titles=("Orders", "Picklist"))
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = _execute_step(
            "GS-08",
            {"url": SHEET_URL, **picklist_table},
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert out["sheet_name"] == "Orders"
    assert values_api.update.call_args.kwargs["range"].startswith("'Orders'!A1:")


def test_gs08_loud_fail_when_named_sheet_missing(picklist_table):
    service, values_api = _mock_service(titles=("Sheet1", "Picklist"))
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=service):
        with pytest.raises(StepExecutionError) as exc:
            _execute_step(
                "GS-08",
                {
                    "url": SHEET_URL,
                    "sheet_name": "DoesNotExist",
                    **picklist_table,
                },
                user_id="u1",
                agent_id="aria",
                agent_name="Aria",
            )
    assert "DoesNotExist" in str(exc.value)
    assert "not found" in str(exc.value).lower()
    values_api.update.assert_not_called()


def test_gs08_requires_url_or_spreadsheet_id():
    with pytest.raises(StepExecutionError) as exc:
        _execute_step(
            "GS-08",
            {"rows": [{"A": "1"}], "columns": ["A"]},
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert "url" in str(exc.value).lower() or "spreadsheet_id" in str(exc.value).lower()
