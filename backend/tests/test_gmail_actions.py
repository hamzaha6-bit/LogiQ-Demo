"""Track A: real Gmail action helpers (mocked Google service)."""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

import google_oauth  # noqa: E402
from google_oauth import build_gmail_query  # noqa: E402
from workflow_runner import StepExecutionError, _execute_step  # noqa: E402


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


# ── build_gmail_query (pure) ────────────────────────────────────────────────

def test_build_query_combines_filters():
    q = build_gmail_query({
        "from": "billing@acme.com",
        "subject": "invoice",
        "after": "2026-07-01",
        "has_attachment": True,
    })
    assert "from:billing@acme.com" in q
    assert "subject:invoice" in q
    assert "after:2026/07/01" in q
    assert "has:attachment" in q


def test_build_query_quotes_multiword_and_supports_freeform():
    q = build_gmail_query({"subject": "past due", "query": "urgent"})
    assert 'subject:"past due"' in q
    assert "urgent" in q


def test_search_requires_a_filter():
    with pytest.raises(ValueError):
        google_oauth.search_messages("u1", {})


# ── GM-07 search via runner ─────────────────────────────────────────────────

def test_gm07_search_builds_query_and_lists():
    service = MagicMock()
    service.users().messages().list().execute.return_value = {
        "messages": [{"id": "m1", "threadId": "t1"}, {"id": "m2", "threadId": "t2"}],
        "resultSizeEstimate": 2,
    }
    with patch("google_oauth.get_gmail_service", return_value=service):
        out = _execute_step(
            "GM-07",
            {"from": "billing@acme.com", "subject": "invoice", "max_results": 5},
            user_id="u1", agent_id="aria", agent_name="Aria",
        )
    assert out["count"] == 2
    assert out["message_ids"] == ["m1", "m2"]
    assert "from:billing@acme.com" in out["built_query"]
    assert "subject:invoice" in out["built_query"]


# ── GM-02 read extracts subject/sender/body ────────────────────────────────

def test_gm02_read_extracts_fields():
    service = MagicMock()
    service.users().messages().get().execute.return_value = {
        "id": "m1", "threadId": "t1", "snippet": "hello",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "Your invoice"},
                {"name": "From", "value": "billing@acme.com"},
                {"name": "To", "value": "me@co.com"},
            ],
            "body": {"data": _b64("Amount due: 500")},
        },
    }
    with patch("google_oauth.get_gmail_service", return_value=service):
        out = _execute_step("GM-02", {"message_id": "m1"}, user_id="u1", agent_id="aria", agent_name="Aria")
    assert out["subject"] == "Your invoice"
    assert out["from"] == "billing@acme.com"
    assert "Amount due: 500" in out["body"]


def test_gm02_requires_message_id():
    with patch("google_oauth.get_gmail_service", return_value=MagicMock()):
        with pytest.raises(StepExecutionError):
            _execute_step("GM-02", {}, user_id="u1", agent_id="aria", agent_name="Aria")


# ── GM-01 list ──────────────────────────────────────────────────────────────

def test_gm01_list_messages():
    service = MagicMock()
    service.users().messages().list().execute.return_value = {
        "messages": [{"id": "m1"}], "resultSizeEstimate": 1,
    }
    with patch("google_oauth.get_gmail_service", return_value=service):
        out = _execute_step("GM-01", {"query": "is:unread"}, user_id="u1", agent_id="aria", agent_name="Aria")
    assert out["count"] == 1


# ── GM-05 draft (creates, does not send) ────────────────────────────────────

def test_gm05_creates_draft_not_send():
    service = MagicMock()
    service.users().drafts().create().execute.return_value = {"id": "d1", "message": {"id": "m9"}}
    with patch("google_oauth.build", return_value=service), \
         patch("google_oauth.get_credentials", return_value=MagicMock()), \
         patch("google_oauth.check_gmail_health", return_value={"email": "me@co.com"}):
        out = _execute_step(
            "GM-05",
            {"to": "client@x.com", "subject": "Hi", "body": "Draft body"},
            user_id="u1", agent_id="aria", agent_name="Aria",
        )
    assert out["draft_id"] == "d1"
    assert out["created"] is True
    # Must not have called send.
    assert not service.users().messages().send.called


def test_gm05_requires_to_and_subject():
    with pytest.raises(StepExecutionError):
        _execute_step("GM-05", {"to": "", "subject": ""}, user_id="u1", agent_id="aria", agent_name="Aria")


# ── GM-06 label modify (resolves names to ids) ──────────────────────────────

def test_gm06_resolves_and_modifies_labels():
    service = MagicMock()
    service.users().labels().list().execute.return_value = {
        "labels": [{"id": "Label_1", "name": "Invoices"}]
    }
    service.users().messages().modify().execute.return_value = {
        "id": "m1", "labelIds": ["Label_1"]
    }
    with patch("google_oauth.get_gmail_service", return_value=service):
        out = _execute_step(
            "GM-06",
            {"message_id": "m1", "add_labels": ["Invoices"], "remove_labels": ["UNREAD"]},
            user_id="u1", agent_id="aria", agent_name="Aria",
        )
    assert out["modified"] is True
    assert "Label_1" in out["added"]
    assert "UNREAD" in out["removed"]


# ── GM-08 thread ────────────────────────────────────────────────────────────

def test_gm08_get_thread():
    service = MagicMock()
    service.users().threads().get().execute.return_value = {
        "id": "t1",
        "messages": [
            {"id": "m1", "snippet": "hi", "payload": {"headers": [{"name": "Subject", "value": "S1"}], "body": {"data": _b64("b1")}}},
        ],
    }
    with patch("google_oauth.get_gmail_service", return_value=service):
        out = _execute_step("GM-08", {"thread_id": "t1"}, user_id="u1", agent_id="aria", agent_name="Aria")
    assert out["count"] == 1
    assert out["messages"][0]["subject"] == "S1"


# ── API errors surface as StepExecutionError ────────────────────────────────

def test_gmail_api_error_raises_step_error():
    service = MagicMock()
    service.users().messages().list().execute.side_effect = RuntimeError("boom")
    with patch("google_oauth.get_gmail_service", return_value=service):
        with pytest.raises(StepExecutionError):
            _execute_step("GM-01", {"query": "x"}, user_id="u1", agent_id="aria", agent_name="Aria")
