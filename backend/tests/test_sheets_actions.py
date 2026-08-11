"""Track B: real Sheets workflow actions (mocked Google + Supabase)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from sheets_service import SheetsError, connect  # noqa: E402
from workflow_runner import StepExecutionError, _execute_step  # noqa: E402

SHEET_URL = "https://docs.google.com/spreadsheets/d/abc123/edit"


def _conn():
    return {
        "id": "conn-1",
        "status": "active",
        "locked_schema": {"column_names": ["Name", "Email"], "columns": [{"name": "Name"}, {"name": "Email"}]},
        "poll_cursor": 1,
        "schema_mismatch": None,
    }


# ── GS-05 connect must fail loudly if DB insert fails ───────────────────────

def test_connect_fails_when_rest_post_returns_no_row():
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.client_id_from_user_id", return_value="client-1"), \
         patch("sheets_service.get_sheets_service", return_value=MagicMock()), \
         patch("sheets_service._resolve_sheet_title", return_value="Sheet1"), \
         patch("sheets_service._fetch_values", return_value=[["Name", "Email"], ["Ada", "a@x.com"]]), \
         patch("sheets_service.rest_post_with_error", return_value=(None, "HTTP 500: boom")):
        with pytest.raises(SheetsError) as exc:
            connect(SHEET_URL, "aria", "u1")
    assert "persist" in str(exc.value).lower() or "boom" in str(exc.value)


def test_connect_includes_client_id():
    """sheet_connections.client_id is NOT NULL — same bug class as workflows/user_integrations."""
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.client_id_from_user_id", return_value="client-1") as mock_cid, \
         patch("sheets_service.get_sheets_service", return_value=MagicMock()), \
         patch("sheets_service._resolve_sheet_title", return_value="Sheet1"), \
         patch("sheets_service._fetch_values", return_value=[["Name"], ["Ada"]]), \
         patch("sheets_service.rest_post_with_error", return_value=({"id": "c1"}, "")) as mock_post:
        out = connect(SHEET_URL, "nova", "u1")
    assert out["success"] is True
    mock_cid.assert_called_once_with("u1")
    payload = mock_post.call_args.args[1]
    assert payload["client_id"] == "client-1"
    assert payload["user_id"] == "u1"
    assert payload["agent_id"] == "nova"


def test_connect_fails_without_client_membership():
    with patch("sheets_service._require_sheets"), \
         patch(
             "sheets_service.client_id_from_user_id",
             side_effect=ValueError("no client membership for user u1"),
         ), \
         patch("sheets_service.rest_post_with_error") as mock_post:
        with pytest.raises(SheetsError) as exc:
            connect(SHEET_URL, "aria", "u1")
    assert "no client membership" in str(exc.value)
    mock_post.assert_not_called()


def test_gs05_connect_success_requires_connection_id():
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.client_id_from_user_id", return_value="client-1"), \
         patch("sheets_service.get_sheets_service", return_value=MagicMock()), \
         patch("sheets_service._resolve_sheet_title", return_value="Sheet1"), \
         patch("sheets_service._fetch_values", return_value=[["Name"], ["Ada"]]), \
         patch("sheets_service.rest_post_with_error", return_value=({"id": "c1"}, "")) as mock_post:
        out = _execute_step(
            "GS-05", {"url": SHEET_URL}, user_id="u1", agent_id="aria", agent_name="Aria"
        )
    assert out["success"] is True
    assert out["connection_id"] == "c1"
    assert out["source_sheet_name"] == "Sheet1"
    payload = mock_post.call_args.args[1]
    assert payload["source_sheet_name"] == "Sheet1"
    assert payload["client_id"] == "client-1"


# ── GS-01 auto-connects when sheet_connections row is missing ───────────────

def test_gs01_autoconnects_when_connection_missing():
    """Integrations UI shows OAuth; GS-01 should create the schema lock itself."""
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_connection", side_effect=[None, _conn()]), \
         patch(
             "sheets_service.connect",
             return_value={"success": True, "connection_id": "conn-1"},
         ) as mock_connect, \
         patch(
             "sheets_service._fetch_values",
             return_value=[["Name", "Email"], ["Ada", "a@x.com"]],
         ):
        out = _execute_step(
            "GS-01",
            {"url": SHEET_URL},
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert out["success"] is True
    assert out["row_count"] == 1
    mock_connect.assert_called_once_with(SHEET_URL, "aria", "u1", sheet_name=None)


def test_gs01_skips_autoconnect_when_already_connected():
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_connection", return_value=_conn()) as mock_get, \
         patch("sheets_service.connect") as mock_connect, \
         patch(
             "sheets_service._fetch_values",
             return_value=[["Name", "Email"], ["Ada", "a@x.com"]],
         ):
        out = _execute_step(
            "GS-01",
            {"url": SHEET_URL},
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert out["success"] is True
    mock_connect.assert_not_called()
    assert mock_get.called


def test_gs01_autoconnect_propagates_connect_failure():
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_connection", return_value=None), \
         patch(
             "sheets_service.connect",
             side_effect=SheetsError("Connect Google first — /api/auth/gmail/connect"),
         ):
        with pytest.raises(StepExecutionError) as exc:
            _execute_step(
                "GS-01",
                {"url": SHEET_URL},
                user_id="u1",
                agent_id="aria",
                agent_name="Aria",
            )
    assert "Connect Google" in str(exc.value) or "not connected" in str(exc.value).lower()


# ── GS-02 append verifies API confirmation ──────────────────────────────────

def test_gs02_append_requires_update_confirmation():
    service = MagicMock()
    service.spreadsheets().values().append().execute.return_value = {"updates": {}}
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_connection", return_value=_conn()), \
         patch("sheets_service._fetch_values", return_value=[["Name", "Email"]]), \
         patch("sheets_service.get_sheets_service", return_value=service):
        with pytest.raises(StepExecutionError):
            _execute_step(
                "GS-02",
                {"url": SHEET_URL, "row": {"Name": "Ada", "Email": "a@x.com"}},
                user_id="u1", agent_id="aria", agent_name="Aria",
            )


def test_gs02_append_success():
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
            user_id="u1", agent_id="aria", agent_name="Aria",
        )
    assert out["success"] is True
    assert out["updated_rows"] == 1


# ── GS-03 update row ────────────────────────────────────────────────────────

def test_gs03_update_row():
    service = MagicMock()
    service.spreadsheets().values().update().execute.return_value = {
        "updatedRange": "A3:B3", "updatedCells": 2
    }
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_connection", return_value=_conn()), \
         patch("sheets_service._fetch_values", return_value=[["Name", "Email"], ["x", "y"], ["a", "b"]]), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = _execute_step(
            "GS-03",
            {"url": SHEET_URL, "row": 3, "row_data": {"Name": "Bob", "Email": "b@x.com"}},
            user_id="u1", agent_id="aria", agent_name="Aria",
        )
    assert out["row"] == 3
    assert out["success"] is True


# ── GS-07 write cell ────────────────────────────────────────────────────────

def test_gs07_write_cell():
    service = MagicMock()
    service.spreadsheets().values().update().execute.return_value = {
        "updatedRange": "B3", "updatedCells": 1
    }
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_connection", return_value=_conn()), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = _execute_step(
            "GS-07",
            {"url": SHEET_URL, "cell": "B3", "value": "done"},
            user_id="u1", agent_id="aria", agent_name="Aria",
        )
    assert out["cell"] == "B3"
    assert out["value"] == "done"


# ── GS-06 delete row ────────────────────────────────────────────────────────

def test_gs06_delete_row():
    service = MagicMock()
    service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}]
    }
    service.spreadsheets().batchUpdate().execute.return_value = {"replies": [{}]}
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_connection", return_value=_conn()), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = _execute_step(
            "GS-06",
            {"url": SHEET_URL, "row": 4},
            user_id="u1", agent_id="aria", agent_name="Aria",
        )
    assert out["deleted"] is True
    assert out["row"] == 4
    assert out["sheet_name"] == "Sheet1"


def test_gs06_refuses_header_row():
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_connection", return_value=_conn()):
        with pytest.raises(StepExecutionError):
            _execute_step(
                "GS-06", {"url": SHEET_URL, "row": 1},
                user_id="u1", agent_id="aria", agent_name="Aria",
            )


# ── GS-04 poll ──────────────────────────────────────────────────────────────

def test_gs04_poll_returns_new_rows_and_advances_cursor():
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_connection", return_value=_conn()), \
         patch("sheets_service._fetch_values", return_value=[
             ["Name", "Email"], ["Ada", "a@x.com"], ["Bob", "b@x.com"]
         ]), \
         patch("sheets_service.rest_patch", return_value=True) as mock_patch:
        out = _execute_step(
            "GS-04", {"url": SHEET_URL}, user_id="u1", agent_id="aria", agent_name="Aria"
        )
    assert out["new_count"] == 2
    assert mock_patch.called
