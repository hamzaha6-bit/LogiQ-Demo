"""Tests for deployable-agent activation gate."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from client_agents import DEPLOYABLE_AGENTS, activate_agent_for_user  # noqa: E402

USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CLIENT_ID = "11111111-2222-4333-8444-555555555555"


def test_deployable_agents_match_workflow_create():
    assert DEPLOYABLE_AGENTS == frozenset({"aria", "nova"})


def test_activate_rejects_finn():
    status, payload = activate_agent_for_user(USER_ID, "finn")
    assert status == 400
    assert payload.get("error") == "agent_not_deployable"
    assert "aria or nova" in payload["detail"]


def test_activate_rejects_zara_and_cleo():
    for aid in ("zara", "cleo"):
        status, payload = activate_agent_for_user(USER_ID, aid)
        assert status == 400
        assert payload.get("error") == "agent_not_deployable"


@patch("client_agents.rest_post", return_value={"id": "row-1"})
@patch("client_agents.count_active_agents", return_value=0)
@patch("client_agents.is_agent_active", return_value=False)
@patch(
    "client_agents.get_entitlement",
    return_value={
        "client_id": CLIENT_ID,
        "status": "active",
        "plan": "starter",
        "agents_limit": 2,
    },
)
@patch("client_agents.client_id_from_user_id", return_value=CLIENT_ID)
def test_activate_allows_aria(
    mock_client: MagicMock,
    mock_ent: MagicMock,
    mock_active: MagicMock,
    mock_count: MagicMock,
    mock_post: MagicMock,
) -> None:
    status, payload = activate_agent_for_user(USER_ID, "aria")
    assert status == 200
    assert payload.get("activated") is True
    assert payload.get("agent_id") == "aria"
