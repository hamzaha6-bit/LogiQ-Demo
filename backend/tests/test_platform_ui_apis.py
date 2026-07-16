"""Unit tests for admin owner check and workflow query helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from admin_dashboard import email_is_owner, build_admin_dashboard  # noqa: E402
from workflow_create import create_workflow_for_user  # noqa: E402
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


def test_create_workflow_requires_auth():
    status, payload = create_workflow_for_user("", {"agent_id": "aria", "steps": [{"step": 1}]})
    assert status == 401


def test_create_workflow_validates_agent():
    status, payload = create_workflow_for_user("user-1", {"agent_id": "cleo", "steps": [{"step": 1}]})
    assert status == 400
    assert "agent_id" in payload["detail"]


def test_create_workflow_requires_steps():
    status, payload = create_workflow_for_user("user-1", {"agent_id": "aria", "steps": []})
    assert status == 400
    assert "steps" in payload["detail"]


@patch("workflow_create.rest_post_with_error")
def test_create_workflow_success(mock_post):
    mock_post.return_value = (
        {
            "id": "wf-new",
            "user_id": "user-1",
            "agent_id": "aria",
            "name": "Remind patients",
            "status": "active",
            "steps": [{"step": 1, "code": "GS-01"}],
        },
        "",
    )
    status, payload = create_workflow_for_user(
        "user-1",
        {
            "agent_id": "aria",
            "name": "Remind patients",
            "description": "Send reminders",
            "trigger_description": "Every morning",
            "steps": [{"step": 1, "code": "GS-01", "name": "Read sheet"}],
            "status": "active",
            "schedule": None,
        },
    )
    assert status == 200
    assert payload["workflow"]["id"] == "wf-new"
    assert mock_post.call_args[0][0] == "workflows"
    inserted = mock_post.call_args[0][1]
    assert inserted["user_id"] == "user-1"
    assert inserted["agent_id"] == "aria"
    assert inserted["name"] == "Remind patients"


@patch("workflow_create.rest_post_with_error")
def test_create_workflow_insert_failure(mock_post):
    mock_post.return_value = (None, "HTTP 500: boom")
    status, payload = create_workflow_for_user(
        "user-1",
        {
            "agent_id": "nova",
            "steps": [{"step": 1, "code": "GM-03"}],
        },
    )
    assert status == 502
    assert "Failed" in payload["detail"] or "boom" in payload["detail"]
