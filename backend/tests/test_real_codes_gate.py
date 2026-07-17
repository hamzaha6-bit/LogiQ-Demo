"""Phase 0: stub codes must hard-fail at create and execute."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from action_registry import REAL_CODES, registry_for_prompt, validate_plan_steps  # noqa: E402
from workflow_create import create_workflow_for_user  # noqa: E402
from workflow_runner import StepExecutionError, _execute_step  # noqa: E402


def test_real_codes_contains_all_21_actions():
    expected = {
        *(f"GM-{i:02d}" for i in range(1, 9)),
        *(f"GS-{i:02d}" for i in range(1, 8)),
        *(f"GC-{i:02d}" for i in range(1, 7)),
    }
    assert REAL_CODES == frozenset(expected)


def test_registry_for_prompt_exposes_only_real_codes():
    codes = {p["code"] for p in registry_for_prompt()}
    assert codes == set(REAL_CODES)
    assert len(codes) == 21


def test_validate_plan_steps_rejects_unknown_code():
    err = validate_plan_steps([{"step": 1, "code": "XX-99", "description": "Unknown"}])
    assert err is not None
    assert "XX-99" in err
    assert "unknown" in err.lower()


def test_validate_plan_steps_allows_real_codes():
    steps = [
        {"step": 1, "code": "gs-01", "params": {"url": "https://example.com"}},
        {"step": 2, "code": "GM-03", "params": {"to": "a@b.com", "subject": "Hi", "body": "x"}},
    ]
    assert validate_plan_steps(steps) is None
    assert steps[0]["code"] == "GS-01"
    assert steps[1]["requires_approval"] is True


@patch("workflow_create.check_execution_gate")
def test_create_workflow_rejects_unknown_code(mock_gate):
    from execution_gate import GateResult

    mock_gate.return_value = GateResult(allowed=True, client_id="c1")
    status, payload = create_workflow_for_user(
        "user-1",
        {
            "agent_id": "aria",
            "steps": [{"step": 1, "code": "XX-99", "description": "Unknown"}],
        },
    )
    assert status == 400
    assert payload.get("error") == "unsupported_step_code"
    assert "XX-99" in payload["detail"]


@patch("workflow_create.check_execution_gate")
@patch("workflow_create.rest_post_with_error")
def test_create_workflow_still_accepts_real_code(mock_post, mock_gate):
    from execution_gate import GateResult

    mock_gate.return_value = GateResult(allowed=True, client_id="c1")
    mock_post.return_value = (
        {"id": "wf-1", "user_id": "user-1", "agent_id": "aria", "steps": [{"step": 1, "code": "GS-01"}]},
        "",
    )
    status, payload = create_workflow_for_user(
        "user-1",
        {"agent_id": "aria", "steps": [{"step": 1, "code": "GS-01", "params": {"url": "https://x"}}]},
    )
    assert status == 200
    assert payload["workflow"]["id"] == "wf-1"


def test_execute_step_hard_fails_on_unknown_code():
    with pytest.raises(StepExecutionError) as exc:
        _execute_step("XX-99", {}, user_id="u1", agent_id="aria", agent_name="Aria")
    assert "XX-99" in str(exc.value)
    assert "not implemented" in str(exc.value).lower()


def test_execute_step_hard_fails_on_missing_code():
    with pytest.raises(StepExecutionError) as exc:
        _execute_step("", {}, user_id="u1", agent_id="aria", agent_name="Aria")
    assert "missing" in str(exc.value)


def test_execute_step_never_returns_logged_true_for_stub():
    with pytest.raises(StepExecutionError):
        out = _execute_step("XX-99", {}, user_id="u1", agent_id="aria", agent_name="Aria")
        assert not (isinstance(out, dict) and out.get("logged") is True)
