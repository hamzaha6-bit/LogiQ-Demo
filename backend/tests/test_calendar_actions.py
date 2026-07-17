"""Track C: real Calendar workflow actions (mocked Google service)."""

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

from workflow_runner import StepExecutionError, _execute_step  # noqa: E402


def _scope_ok(*_a, **_k):
    return True


# ── GC-01 availability ──────────────────────────────────────────────────────

def test_gc01_check_availability():
    service = MagicMock()
    service.freebusy().query().execute.return_value = {
        "calendars": {"primary": {"busy": [{"start": "2026-07-18T10:00:00Z", "end": "2026-07-18T11:00:00Z"}]}}
    }
    with patch("google_oauth.load_user_token", return_value={"token": "x"}), \
         patch("google_oauth.has_scope", side_effect=_scope_ok), \
         patch("google_oauth.get_calendar_service", return_value=service):
        out = _execute_step(
            "GC-01",
            {"time_min": "2026-07-18T00:00:00Z", "time_max": "2026-07-19T00:00:00Z"},
            user_id="u1", agent_id="aria", agent_name="Aria",
        )
    assert out["busy_count"] == 1
    assert out["success"] is True


def test_gc01_requires_time_bounds():
    with patch("google_oauth.load_user_token", return_value={"token": "x"}), \
         patch("google_oauth.has_scope", side_effect=_scope_ok):
        with pytest.raises(StepExecutionError):
            _execute_step("GC-01", {}, user_id="u1", agent_id="aria", agent_name="Aria")


# ── GC-02 list events ───────────────────────────────────────────────────────

def test_gc02_list_events():
    service = MagicMock()
    service.events().list().execute.return_value = {
        "items": [
            {
                "id": "e1",
                "summary": "Demo",
                "start": {"dateTime": "2026-07-18T10:00:00Z"},
                "end": {"dateTime": "2026-07-18T11:00:00Z"},
                "htmlLink": "https://cal.example/e1",
                "attendees": [{"email": "a@x.com"}],
            }
        ]
    }
    with patch("google_oauth.load_user_token", return_value={"token": "x"}), \
         patch("google_oauth.has_scope", side_effect=_scope_ok), \
         patch("google_oauth.get_calendar_service", return_value=service):
        out = _execute_step("GC-02", {"max_results": 5}, user_id="u1", agent_id="aria", agent_name="Aria")
    assert out["count"] == 1
    assert out["events"][0]["event_id"] == "e1"


# ── GC-03 create / GC-06 invite ─────────────────────────────────────────────

def test_gc03_create_event_requires_id():
    service = MagicMock()
    service.events().insert().execute.return_value = {"summary": "No id"}
    with patch("google_oauth.load_user_token", return_value={"token": "x"}), \
         patch("google_oauth.has_scope", side_effect=_scope_ok), \
         patch("google_oauth.get_calendar_service", return_value=service):
        with pytest.raises(StepExecutionError):
            _execute_step(
                "GC-03",
                {"title": "Demo", "start": "2026-07-18T10:00:00Z", "end": "2026-07-18T11:00:00Z"},
                user_id="u1", agent_id="aria", agent_name="Aria",
            )


def test_gc06_invite_requires_attendees_and_sends():
    service = MagicMock()
    service.events().insert().execute.return_value = {
        "id": "e9", "summary": "Call", "htmlLink": "https://cal.example/e9"
    }
    with patch("google_oauth.load_user_token", return_value={"token": "x"}), \
         patch("google_oauth.has_scope", side_effect=_scope_ok), \
         patch("google_oauth.get_calendar_service", return_value=service):
        out = _execute_step(
            "GC-06",
            {
                "title": "Call",
                "start": "2026-07-18T10:00:00Z",
                "end": "2026-07-18T11:00:00Z",
                "attendees": ["client@x.com"],
            },
            user_id="u1", agent_id="aria", agent_name="Aria",
        )
    assert out["event_id"] == "e9"
    assert out["send_updates"] == "all"
    assert "client@x.com" in out["attendees"]


def test_gc06_requires_attendees():
    with patch("google_oauth.load_user_token", return_value={"token": "x"}), \
         patch("google_oauth.has_scope", side_effect=_scope_ok):
        with pytest.raises(StepExecutionError):
            _execute_step(
                "GC-06",
                {"title": "Call", "start": "2026-07-18T10:00:00Z", "end": "2026-07-18T11:00:00Z"},
                user_id="u1", agent_id="aria", agent_name="Aria",
            )


# ── GC-04 update / GC-05 cancel ─────────────────────────────────────────────

def test_gc04_update_event():
    service = MagicMock()
    service.events().get().execute.return_value = {"id": "e1", "summary": "Old"}
    service.events().update().execute.return_value = {"id": "e1", "summary": "New", "htmlLink": "h"}
    with patch("google_oauth.load_user_token", return_value={"token": "x"}), \
         patch("google_oauth.has_scope", side_effect=_scope_ok), \
         patch("google_oauth.get_calendar_service", return_value=service):
        out = _execute_step(
            "GC-04",
            {"event_id": "e1", "title": "New"},
            user_id="u1", agent_id="aria", agent_name="Aria",
        )
    assert out["updated"] is True
    assert out["summary"] == "New"


def test_gc05_cancel_event():
    service = MagicMock()
    service.events().delete().execute.return_value = None
    with patch("google_oauth.load_user_token", return_value={"token": "x"}), \
         patch("google_oauth.has_scope", side_effect=_scope_ok), \
         patch("google_oauth.get_calendar_service", return_value=service):
        out = _execute_step(
            "GC-05",
            {"event_id": "e1"},
            user_id="u1", agent_id="aria", agent_name="Aria",
        )
    assert out["cancelled"] is True
