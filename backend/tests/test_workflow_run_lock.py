"""E16 — reject a second in-flight run for the same workflow."""

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
from workflow_runner import run_workflow_for_user  # noqa: E402

USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
WF_ID = "wf-11111111-2222-4333-8444-555555555555"


@patch("workflow_runner._create_run")
@patch("workflow_runner.rest_get")
@patch("workflow_runner.check_execution_gate")
def test_second_run_blocked_when_already_running(
    mock_gate: MagicMock, mock_get: MagicMock, mock_create: MagicMock
) -> None:
    mock_gate.return_value = GateResult(allowed=True, client_id="c1")

    def _get(table, params=None):
        if table == "workflows":
            return [
                {
                    "id": WF_ID,
                    "user_id": USER_ID,
                    "status": "active",
                    "deleted_at": None,
                    "agent_id": "aria",
                    "steps": [{"step": 1, "code": "GS-01", "params": {}}],
                }
            ]
        if table == "workflow_runs":
            return [{"id": "run-existing", "status": "running"}]
        return []

    mock_get.side_effect = _get
    status, payload = run_workflow_for_user(USER_ID, WF_ID)
    assert status == 409
    assert payload["error"] == "workflow_already_running"
    mock_create.assert_not_called()
