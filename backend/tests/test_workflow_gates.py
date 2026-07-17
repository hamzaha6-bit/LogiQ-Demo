"""Workflow create/run gate coverage for non-paying users."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_dummy")

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from execution_gate import GateResult  # noqa: E402
from workflow_create import create_workflow_for_user  # noqa: E402
from workflow_runner import run_workflow_for_user  # noqa: E402

USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
WF_ID = "wf-11111111-2222-4333-8444-555555555555"


@patch("workflow_create.check_execution_gate")
def test_create_workflow_blocks_non_paying(mock_gate: MagicMock) -> None:
    mock_gate.return_value = GateResult(
        allowed=False,
        reason="Please subscribe to continue using LogiQ.",
        error="no_active_subscription",
        client_id="c1",
    )
    status, payload = create_workflow_for_user(
        USER_ID,
        {"agent_id": "aria", "steps": [{"step": 1, "code": "GS-01", "params": {"url": "https://x"}}]},
    )
    assert status == 403
    assert payload["error"] == "no_active_subscription"
    assert payload["message"] == "Upgrade to deploy and run automations."
    assert payload["detail"] == "Upgrade to deploy and run automations."
    mock_gate.assert_called_once_with(USER_ID, "workflow_create")


@patch("workflow_create.rest_post_with_error")
@patch("workflow_create.check_execution_gate")
def test_create_workflow_allows_when_gate_passes(mock_gate: MagicMock, mock_post: MagicMock) -> None:
    mock_gate.return_value = GateResult(allowed=True, client_id="c1")
    mock_post.return_value = (
        {"id": "wf-1", "user_id": USER_ID, "agent_id": "aria", "steps": [{"step": 1, "code": "GS-01"}]},
        "",
    )
    status, payload = create_workflow_for_user(
        USER_ID,
        {"agent_id": "aria", "steps": [{"step": 1, "code": "GS-01", "params": {"url": "https://x"}}]},
    )
    assert status == 200
    assert payload["workflow"]["id"] == "wf-1"


@patch("workflow_runner.rest_get")
@patch("workflow_runner.check_execution_gate")
def test_resume_path_is_gated(mock_gate: MagicMock, mock_get: MagicMock) -> None:
    mock_gate.return_value = GateResult(
        allowed=False,
        reason="Please subscribe",
        error="no_active_subscription",
        client_id="c1",
    )
    mock_get.return_value = [
        {
            "id": WF_ID,
            "user_id": USER_ID,
            "status": "active",
            "deleted_at": None,
            "agent_id": "aria",
            "steps": [{"step": 1, "code": "GS-01"}],
        }
    ]
    status, payload = run_workflow_for_user(
        USER_ID,
        WF_ID,
        workflow_run_id="run-1",
        approval_id="appr-1",
    )
    assert status == 403
    assert payload["message"] == "Upgrade to deploy and run automations."
    mock_gate.assert_called_once_with(USER_ID, "workflow_run")
