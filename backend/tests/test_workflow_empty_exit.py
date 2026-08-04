"""Tests for empty-search clean exit and first-result template binding."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from execution_gate import GateResult  # noqa: E402
from workflow_context import (  # noqa: E402
    empty_context,
    resolve_params,
    resolve_params_with_meta,
    set_step_output,
)
from workflow_runner import run_workflow_for_user  # noqa: E402

USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
WF_ID = "wf-11111111-2222-4333-8444-555555555555"


def test_alt_template_normalizes_to_first_result():
    ctx = empty_context()
    set_step_output(
        ctx,
        1,
        {
            "results": [{"message_id": "oldest", "thread_id": "t1"}],
            "message_ids": ["oldest"],
            "count": 1,
        },
    )
    resolved = resolve_params({"message_id": "{{step1.results[0].message_id}}"}, ctx)
    assert resolved["message_id"] == "oldest"


def test_empty_results_template_flags_meta():
    ctx = empty_context()
    set_step_output(ctx, 1, {"results": [], "message_ids": [], "count": 0})
    resolved, empty = resolve_params_with_meta(
        {"message_id": "{{step_1.output.results.0.message_id}}"},
        ctx,
    )
    assert resolved["message_id"] == ""
    assert empty is True


@patch("workflow_runner.record_allowed_action")
@patch("workflow_runner._finish_workflow_schedule")
@patch("workflow_runner._save_run")
@patch("workflow_runner._create_run", return_value="run-1")
@patch("workflow_runner._execute_step")
@patch("workflow_runner.rest_get")
@patch("workflow_runner.check_execution_gate")
def test_empty_gm07_exits_cleanly(
    mock_gate: MagicMock,
    mock_get: MagicMock,
    mock_exec: MagicMock,
    _create_run: MagicMock,
    _save: MagicMock,
    _finish: MagicMock,
    _record: MagicMock,
) -> None:
    mock_gate.return_value = GateResult(allowed=True, client_id="c1")
    mock_get.return_value = [
        {
            "id": WF_ID,
            "user_id": USER_ID,
            "status": "active",
            "deleted_at": None,
            "agent_id": "nova",
            "steps": [
                {
                    "step": 1,
                    "code": "GM-07",
                    "params": {
                        "query": (
                            '(franchise OR franchising OR "become a franchisee" OR '
                            '"franchise opportunity") -label:"Franchise Enquiry"'
                        )
                    },
                },
                {
                    "step": 2,
                    "code": "GM-02",
                    "params": {"message_id": "{{step_1.output.results.0.message_id}}"},
                },
            ],
        }
    ]
    mock_exec.return_value = {
        "count": 0,
        "results": [],
        "message_ids": [],
        "order": "oldest_first",
    }
    status, payload = run_workflow_for_user(USER_ID, WF_ID)
    assert status == 200
    assert payload["status"] == "completed_empty"
    assert mock_exec.call_count == 1
