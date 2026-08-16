"""Server-side approval resolve is user-scoped."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from workflow_approvals import resolve_approval_for_user  # noqa: E402

USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@patch("workflow_approvals.rest_patch", return_value=True)
@patch("workflow_approvals.rest_get")
def test_resolve_rejects_unknown_approval(mock_get, mock_patch):
    mock_get.return_value = []
    status, payload = resolve_approval_for_user(USER_ID, "appr-1", "approved")
    assert status == 404
    mock_patch.assert_not_called()
    assert "not found" in payload["detail"].lower()


@patch("workflow_approvals.run_workflow_for_user", return_value=(200, {"status": "completed"}))
@patch("workflow_approvals.rest_patch", return_value=True)
@patch("workflow_approvals.rest_get")
def test_approve_resumes_run(mock_get, mock_patch, mock_run):
    mock_get.return_value = [
        {
            "id": "appr-1",
            "user_id": USER_ID,
            "status": "pending",
            "workflow_id": "wf-1",
            "workflow_run_id": "run-1",
        }
    ]
    status, payload = resolve_approval_for_user(USER_ID, "appr-1", "approved")
    assert status == 200
    assert payload["status"] == "completed"
    mock_run.assert_called_once()
