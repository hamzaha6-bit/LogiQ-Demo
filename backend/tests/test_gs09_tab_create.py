"""Pound Fabrics piece 3: GS-09 tab create + sheet_name targeting (mocked Sheets API)."""

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

from sheets_service import create_sheet  # noqa: E402
from workflow_runner import StepExecutionError, _execute_step  # noqa: E402

SHEET_URL = "https://docs.google.com/spreadsheets/d/abc123/edit"


def _conn(**overrides):
    base = {
        "id": "conn-1",
        "status": "active",
        "source_sheet_name": None,
        "locked_schema": {
            "column_names": ["Name", "Email"],
            "columns": [{"name": "Name"}, {"name": "Email"}],
        },
        "poll_cursor": 1,
        "schema_mismatch": None,
    }
    base.update(overrides)
    return base


def _sheets_meta(*titles: str):
    return {
        "sheets": [
            {"properties": {"sheetId": i, "title": t}} for i, t in enumerate(titles)
        ]
    }


def _mock_service(*, titles=("Sheet1", "Orders")):
    service = MagicMock()
    spreadsheets = service.spreadsheets.return_value
    spreadsheets.get.return_value.execute.return_value = _sheets_meta(*titles)
    return service, spreadsheets


# ── GS-09 create sheet / addSheet ─────────────────────────────────────────────

def test_gs09_creates_sheet_via_addSheet():
    service, spreadsheets = _mock_service(titles=("Sheet1",))
    spreadsheets.batchUpdate.return_value.execute.return_value = {
        "replies": [{"addSheet": {"properties": {"sheetId": 7, "title": "Picklist 1"}}}]
    }
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = _execute_step(
            "GS-09",
            {"url": SHEET_URL, "sheet_name": "Picklist 1"},
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )

    assert out["success"] is True
    assert out["created"] is True
    assert out["created_via"] == "addSheet"
    assert out["sheet_name"] == "Picklist 1"
    assert out["sheet_id"] == 7
    assert out["schema_lock"] is False
    body = spreadsheets.batchUpdate.call_args.kwargs["body"]
    assert body["requests"][0]["addSheet"]["properties"]["title"] == "Picklist 1"


def test_gs09_duplicates_template_with_newSheetName():
    service, spreadsheets = _mock_service(titles=("Sheet1", "Picklist Template"))
    spreadsheets.batchUpdate.return_value.execute.return_value = {
        "replies": [
            {
                "duplicateSheet": {
                    "properties": {"sheetId": 42, "title": "Picklist 1"}
                }
            }
        ]
    }
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = _execute_step(
            "GS-09",
            {
                "url": SHEET_URL,
                "sheet_name": "Picklist 1",
                "template_sheet_name": "Picklist Template",
            },
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )

    assert out["success"] is True
    assert out["created_via"] == "duplicate"
    assert out["template_sheet_name"] == "Picklist Template"
    assert out["sheet_id"] == 42
    body = spreadsheets.batchUpdate.call_args.kwargs["body"]
    dup = body["requests"][0]["duplicateSheet"]
    assert dup["sourceSheetId"] == 1  # second tab in mock meta
    assert dup["newSheetName"] == "Picklist 1"
    assert "addSheet" not in body["requests"][0]


def test_gs09_template_missing_hard_fails_no_addSheet():
    service, spreadsheets = _mock_service(titles=("Sheet1",))
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=service):
        with pytest.raises(StepExecutionError) as exc:
            _execute_step(
                "GS-09",
                {
                    "url": SHEET_URL,
                    "sheet_name": "Picklist 1",
                    "template_sheet_name": "Missing Template",
                },
                user_id="u1",
                agent_id="aria",
                agent_name="Aria",
            )
    assert "Missing Template" in str(exc.value)
    assert "not found" in str(exc.value).lower()
    spreadsheets.batchUpdate.assert_not_called()


def test_gs09_accepts_title_alias():
    service, spreadsheets = _mock_service(titles=("Sheet1",))
    spreadsheets.batchUpdate.return_value.execute.return_value = {
        "replies": [{"addSheet": {"properties": {"sheetId": 2, "title": "Exceptions"}}}]
    }
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = _execute_step(
            "GS-09",
            {"url": SHEET_URL, "title": "Exceptions"},
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert out["sheet_name"] == "Exceptions"


def test_gs09_loud_fail_when_sheet_already_exists():
    service, spreadsheets = _mock_service(titles=("Sheet1", "Picklist"))
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=service):
        with pytest.raises(StepExecutionError) as exc:
            _execute_step(
                "GS-09",
                {"url": SHEET_URL, "sheet_name": "Picklist"},
                user_id="u1",
                agent_id="aria",
                agent_name="Aria",
            )
    assert "already exists" in str(exc.value).lower()
    spreadsheets.batchUpdate.assert_not_called()


def test_gs09_requires_sheet_name():
    with pytest.raises(StepExecutionError) as exc:
        _execute_step(
            "GS-09",
            {"url": SHEET_URL},
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert "sheet_name" in str(exc.value).lower()


def test_create_sheet_helper_requires_title():
    with patch("sheets_service._require_sheets"):
        with pytest.raises(Exception) as exc:
            create_sheet(SHEET_URL, "u1", "  ")
    assert "sheet_name" in str(exc.value).lower()


# ── Read / write / delete with sheet_name ─────────────────────────────────────

def test_gs01_reads_named_sheet():
    service, _spreadsheets = _mock_service(titles=("Sheet1", "Orders"))
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_connection", return_value=_conn()), \
         patch("sheets_service.get_sheets_service", return_value=service), \
         patch(
             "sheets_service._fetch_values",
             return_value=[["Name", "Email"], ["Ada", "a@x.com"]],
         ) as mock_fetch:
        out = _execute_step(
            "GS-01",
            {"url": SHEET_URL, "sheet_name": "Orders"},
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert out["success"] is True
    assert out["sheet_name"] == "Orders"
    assert out["row_count"] == 1
    assert mock_fetch.call_args.args[2] == "Orders"


def test_gs01_loud_fail_when_named_sheet_missing():
    service, _spreadsheets = _mock_service(titles=("Sheet1", "Orders"))
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_connection", return_value=_conn()), \
         patch("sheets_service.get_sheets_service", return_value=service):
        with pytest.raises(StepExecutionError) as exc:
            _execute_step(
                "GS-01",
                {"url": SHEET_URL, "sheet_name": "DoesNotExist"},
                user_id="u1",
                agent_id="aria",
                agent_name="Aria",
            )
    assert "DoesNotExist" in str(exc.value)
    assert "not found" in str(exc.value).lower()


def test_gs02_append_targets_named_sheet():
    service, spreadsheets = _mock_service(titles=("Sheet1", "Orders"))
    spreadsheets.values.return_value.append.return_value.execute.return_value = {
        "updates": {"updatedRange": "'Orders'!A2:B2", "updatedRows": 1}
    }
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_connection", return_value=_conn()), \
         patch(
             "sheets_service._fetch_values",
             return_value=[["Name", "Email"]],
         ), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = _execute_step(
            "GS-02",
            {
                "url": SHEET_URL,
                "sheet_name": "Orders",
                "row": {"Name": "Ada", "Email": "a@x.com"},
            },
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert out["success"] is True
    assert out["sheet_name"] == "Orders"
    kwargs = spreadsheets.values.return_value.append.call_args.kwargs
    assert kwargs["range"].startswith("'Orders'!")


def test_gs06_delete_targets_named_sheet():
    service, spreadsheets = _mock_service(titles=("Sheet1", "Orders"))
    spreadsheets.batchUpdate.return_value.execute.return_value = {"replies": [{}]}
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_connection", return_value=_conn()), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = _execute_step(
            "GS-06",
            {"url": SHEET_URL, "row": 3, "sheet_name": "Orders"},
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert out["deleted"] is True
    assert out["sheet_name"] == "Orders"
    assert out["sheet_id"] == 1  # second tab in mock meta
    req = spreadsheets.batchUpdate.call_args.kwargs["body"]["requests"][0]
    assert req["deleteDimension"]["range"]["sheetId"] == 1


def test_gs06_loud_fail_when_named_sheet_missing():
    service, spreadsheets = _mock_service(titles=("Sheet1",))
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_connection", return_value=_conn()), \
         patch("sheets_service.get_sheets_service", return_value=service):
        with pytest.raises(StepExecutionError) as exc:
            _execute_step(
                "GS-06",
                {"url": SHEET_URL, "row": 3, "sheet_name": "Missing"},
                user_id="u1",
                agent_id="aria",
                agent_name="Aria",
            )
    assert "Missing" in str(exc.value)
    spreadsheets.batchUpdate.assert_not_called()


# ── Default first-tab unchanged ───────────────────────────────────────────────

def test_gs01_defaults_first_tab_when_sheet_name_omitted():
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_connection", return_value=_conn()), \
         patch(
             "sheets_service._fetch_values",
             return_value=[["Name", "Email"], ["Ada", "a@x.com"]],
         ) as mock_fetch:
        out = _execute_step(
            "GS-01",
            {"url": SHEET_URL},
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert out["success"] is True
    assert "sheet_name" not in out  # unnamed first-tab path
    # None / omitted → first-tab fetch (unqualified A:ZZ inside helper)
    assert mock_fetch.call_args.args[2] is None


def test_gs02_default_append_uses_unqualified_range():
    service = MagicMock()
    service.spreadsheets().values().append().execute.return_value = {
        "updates": {"updatedRange": "Sheet1!A2:B2", "updatedRows": 1}
    }
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_connection", return_value=_conn()), \
         patch("sheets_service._fetch_values", return_value=[["Name", "Email"]]), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = _execute_step(
            "GS-02",
            {"url": SHEET_URL, "row_data": {"Name": "Ada", "Email": "a@x.com"}},
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert out["success"] is True
    kwargs = service.spreadsheets().values().append.call_args.kwargs
    assert kwargs["range"] == "A:ZZ"


# ── Connection source tab distinction ─────────────────────────────────────────

def test_gs05_stores_source_sheet_name_when_named():
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=MagicMock()), \
         patch("sheets_service._resolve_sheet_title", return_value="Orders"), \
         patch("sheets_service._fetch_values", return_value=[["Name"], ["Ada"]]), \
         patch("sheets_service.rest_post_with_error", return_value=({"id": "c1"}, "")) as mock_post:
        out = _execute_step(
            "GS-05",
            {"url": SHEET_URL, "sheet_name": "Orders"},
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert out["source_sheet_name"] == "Orders"
    assert mock_post.call_args.args[1]["source_sheet_name"] == "Orders"


def test_gs01_uses_connection_source_sheet_when_param_omitted():
    with patch("sheets_service._require_sheets"), \
         patch(
             "sheets_service.get_connection",
             return_value=_conn(source_sheet_name="Orders"),
         ), \
         patch("sheets_service._resolve_sheet_title", return_value="Orders"), \
         patch(
             "sheets_service._fetch_values",
             return_value=[["Name", "Email"], ["Ada", "a@x.com"]],
         ) as mock_fetch, \
         patch("sheets_service.get_sheets_service", return_value=MagicMock()):
        out = _execute_step(
            "GS-01",
            {"url": SHEET_URL},
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert out["sheet_name"] == "Orders"
    assert mock_fetch.call_args.args[2] == "Orders"
