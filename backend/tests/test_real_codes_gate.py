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


def test_real_codes_are_exactly_the_three_implemented():
    assert REAL_CODES == frozenset({"GS-01", "GM-03", "GM-04"})


def test_registry_for_prompt_exposes_only_real_codes():
    codes = {p["code"] for p in registry_for_prompt()}
    assert codes == set(REAL_CODES)
    assert "GM-07" not in codes
    assert "GC-01" not in codes


def test_validate_plan_steps_rejects_stub_code():
    err = validate_plan_steps([{"step": 1, "code": "GM-07", "description": "Search inbox"}])
    assert err is not None
    assert "GM-07" in err
    assert "not available" in err.lower() or "available" in err.lower()


def test_validate_plan_steps_allows_real_codes():
    steps = [
        {"step": 1, "code": "gs-01", "params": {"url": "https://example.com"}},
        {"step": 2, "code": "GM-03", "params": {"to": "a@b.com", "subject": "Hi", "body": "x"}},
    ]
    assert validate_plan_steps(steps) is None
    assert steps[0]["code"] == "GS-01"
    assert steps[1]["requires_approval"] is True


def test_create_workflow_rejects_stub_code():
    status, payload = create_workflow_for_user(
        "user-1",
        {
            "agent_id": "aria",
            "steps": [{"step": 1, "code": "GM-07", "description": "Find invoice"}],
        },
    )
    assert status == 400
    assert payload.get("error") == "unsupported_step_code"
    assert "GM-07" in payload["detail"]


@patch("workflow_create.rest_post_with_error")
def test_create_workflow_still_accepts_real_code(mock_post):
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


def test_execute_step_hard_fails_on_stub_code():
    with pytest.raises(StepExecutionError) as exc:
        _execute_step("GM-07", {}, user_id="u1", agent_id="aria", agent_name="Aria")
    assert "GM-07" in str(exc.value)
    assert "not implemented" in str(exc.value).lower()


def test_execute_step_hard_fails_on_unknown_code():
    with pytest.raises(StepExecutionError) as exc:
        _execute_step("XX-99", {}, user_id="u1", agent_id="aria", agent_name="Aria")
    assert "XX-99" in str(exc.value)


def test_execute_step_never_returns_logged_true_for_stub():
    with pytest.raises(StepExecutionError):
        out = _execute_step("GM-02", {}, user_id="u1", agent_id="aria", agent_name="Aria")
        assert not (isinstance(out, dict) and out.get("logged") is True)
