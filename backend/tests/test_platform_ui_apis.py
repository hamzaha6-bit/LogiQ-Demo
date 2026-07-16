"""Unit tests for admin owner check and workflow query helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from admin_dashboard import email_is_owner, build_admin_dashboard  # noqa: E402
from workflow_queries import latest_run_for_user_workflow, list_workflows_for_user  # noqa: E402


def test_email_is_owner_respects_env(monkeypatch):
    monkeypatch.setenv("OWNER_EMAILS", "hamza@logiq.org.uk, other@example.com")
    assert email_is_owner("hamza@logiq.org.uk") is True
    assert email_is_owner("HAMZA@logiq.org.uk") is True
    assert email_is_owner("stranger@example.com") is False
    assert email_is_owner("") is False


def test_email_is_owner_empty_env(monkeypatch):
    monkeypatch.delenv("OWNER_EMAILS", raising=False)
    assert email_is_owner("hamza@logiq.org.uk") is False


@patch("admin_dashboard.user_is_owner", return_value=False)
def test_admin_dashboard_forbidden_for_non_owner(mock_owner):
    status, payload = build_admin_dashboard("user-1")
    assert status == 403
    assert "Owner" in payload["detail"]


@patch("workflow_queries.rest_get")
def test_list_workflows_attaches_last_run(mock_get):
    mock_get.side_effect = [
        [{"id": "wf1", "name": "Chase invoices", "agent_id": "aria", "status": "active"}],
        [{"id": "run1", "status": "completed", "started_at": "2026-07-01T10:00:00Z", "completed_at": "2026-07-01T10:01:00Z", "context_json": {"step_1": {"output": {"rows": 3}}}, "error": None}],
    ]
    status, payload = list_workflows_for_user("user-1")
    assert status == 200
    assert len(payload["workflows"]) == 1
    assert payload["workflows"][0]["last_run"]["id"] == "run1"
    assert payload["workflows"][0]["last_run"]["context_json"]["step_1"]["output"]["rows"] == 3


@patch("workflow_queries.rest_get")
def test_latest_run_not_owned(mock_get):
    mock_get.return_value = []
    status, payload = latest_run_for_user_workflow("user-1", "wf-missing")
    assert status == 404


@patch("workflow_queries.rest_get")
def test_latest_run_null_when_no_runs(mock_get):
    mock_get.side_effect = [
        [{"id": "wf1"}],
        [],
    ]
    status, payload = latest_run_for_user_workflow("user-1", "wf1")
    assert status == 200
    assert payload["run"] is None
