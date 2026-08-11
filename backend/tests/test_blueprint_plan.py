"""Tests for Blueprint plan parsing and user-facing summaries."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from blueprint_plan import (  # noqa: E402
    build_user_facing_summary,
    format_approval_payload,
    inherit_sheet_bindings,
    parse_blueprint_plan,
    plain_approval_label,
    plain_step_label,
    prepare_blueprint_response,
    strip_plan_json,
    summary_leaks_internals,
    validate_sheet_bindings,
)

FRANCHISE_PLAN = {
    "supported": True,
    "title": "Franchise Enquiry Auto-Response",
    "summary": "Processes one franchise enquiry per run when mail arrives.",
    "agent": "nova",
    "steps": [
        {
            "step": 1,
            "code": "GM-07",
            "description": "Find franchise enquiries that are not labelled yet",
            "params": {
                "query": (
                    '(franchise OR franchising OR "become a franchisee" OR "franchise opportunity") '
                    '-label:"Franchise Enquiry"'
                ),
                "max_results": 10,
            },
        },
        {
            "step": 2,
            "code": "GM-02",
            "description": "Read the oldest matching enquiry",
            "params": {"message_id": "{{step_1.output.results.0.message_id}}"},
        },
        {
            "step": 3,
            "code": "GM-03",
            "description": "Send an immediate acknowledgement to the sender",
            "params": {
                "to": "{{step_2.output.from}}",
                "subject": "Thanks for your franchise enquiry",
                "body": "We received your message.",
            },
        },
        {
            "step": 4,
            "code": "GM-06",
            "description": "Label the email so it's easy to find later",
            "params": {
                "message_id": "{{step_1.output.results.0.message_id}}",
                "add_labels": ["Franchise Enquiry"],
            },
        },
        {
            "step": 5,
            "code": "GS-02",
            "description": "Log it in your tracking spreadsheet",
            "params": {"url": "", "row": {"email": "{{step_2.output.from}}"}},
        },
        {
            "step": 6,
            "code": "GM-05",
            "description": "Prepare a follow-up draft for you to personalise and send",
            "params": {
                "to": "{{step_2.output.from}}",
                "subject": "Following up",
                "body": "Draft follow-up",
            },
        },
    ],
}


def test_parse_fenced_plan():
    text = (
        "Happy to help.\n\n```json\n"
        + __import__("json").dumps(FRANCHISE_PLAN)
        + "\n```"
    )
    plan = parse_blueprint_plan(text)
    assert plan is not None
    assert plan["title"] == "Franchise Enquiry Auto-Response"
    assert plan["steps"][0]["params"]["query"].endswith('-label:"Franchise Enquiry"')


def test_strip_removes_json_and_codes_from_display_path():
    text = "Sure — here is a plan.\n\n```json\n" + __import__("json").dumps(FRANCHISE_PLAN) + "\n```"
    stripped = strip_plan_json(text)
    assert "supported" not in stripped
    assert "GM-07" not in stripped
    assert "{{" not in stripped


def test_user_facing_summary_has_no_internals():
    summary = build_user_facing_summary(FRANCHISE_PLAN)
    assert "Franchise Enquiry Auto-Response" in summary
    assert "Nova" in summary
    assert "needs your approval" in summary
    assert "spreadsheet link" in summary.lower() or "spreadsheet" in summary.lower()
    assert "one" in summary.lower() and "per run" in summary.lower()
    assert not summary_leaks_internals(summary)
    assert "GM-07" not in summary
    assert "{{" not in summary
    assert "message_id" not in summary


def test_prepare_returns_plan_separately():
    raw = "I'll set that up.\n\n```json\n" + __import__("json").dumps(FRANCHISE_PLAN) + "\n```"
    content, plan, err = prepare_blueprint_response(raw)
    assert err is None
    assert plan is not None
    assert plan["steps"][0]["code"] == "GM-07"
    assert "supported" not in content
    assert not summary_leaks_internals(content)
    assert "```" not in content


def test_format_approval_payload_email():
    text = format_approval_payload(
        {"to": "a@b.com", "subject": "Hi", "body": "Hello"},
        "GM-03",
    )
    assert "To: a@b.com" in text
    assert "Subject: Hi" in text
    assert "Hello" in text
    assert "{" not in text


def test_format_approval_payload_sheets_uses_plain_language_not_json():
    """Generic third-party / Sheets payload with no dedicated formatter."""
    payload = {
        "url": "https://docs.google.com/spreadsheets/d/abc/edit",
        "row": {"email": "{{step_2.output.from}}", "status": "new"},
        "range": "Sheet1!A1",
    }
    text = format_approval_payload(
        payload,
        "GS-02",
        description="Log it in your tracking spreadsheet",
        action_name="Append row",
        integration="Google Sheets",
    )
    assert "Log it in your tracking spreadsheet" in text
    assert "GS-02" not in text
    assert "{{" not in text
    assert "url" not in text
    assert '"row"' not in text
    assert "{" not in text
    assert not text.strip().startswith("{")


def test_format_approval_payload_generic_without_description():
    text = format_approval_payload(
        {"foo": "bar", "nested": {"x": 1}},
        "GS-03",
        action_name="Update row",
        integration="Google Sheets",
    )
    assert "Google Sheets" in text
    assert "Update row" in text
    assert "This step will run" in text
    assert "foo" not in text
    assert "GS-03" not in text
    assert "{" not in text


def test_format_approval_payload_last_resort_never_json():
    text = format_approval_payload({"secret_key": "xyz"}, "XX-99")
    assert text
    assert "{" not in text
    assert "secret_key" not in text
    assert "XX-99" not in text
    assert "approval" in text.lower() or "step" in text.lower()


def test_plain_approval_label_no_code_step_leak():
    label = plain_approval_label(
        {
            "primitive_code": "GM-06",
            "step_number": 3,
            "summary": "Label the email so it's easy to find later",
            "action_name": "Label message",
        }
    )
    assert "Label the email so it's easy to find later" in label
    assert "GM-06" not in label
    assert "Step 3" not in label
    assert not summary_leaks_internals(label)


def test_plain_step_label_strips_templates_from_description():
    label = plain_step_label(
        {
            "code": "GM-03",
            "description": "Send to {{step_2.output.from}} via GM-03",
        }
    )
    assert "GM-03" not in label
    assert "{{" not in label
    assert "Send to" in label


REAL_SHEET = (
    "https://docs.google.com/spreadsheets/d/"
    "1qlOS1W3p1MIeZKViZEdCLnhXa_8fInsxjGN4Orzo0Ng/edit"
)


def test_inherit_sheet_bindings_replaces_your_sheet_id_on_regenerate():
    prior = {
        "supported": True,
        "title": "Picklist",
        "agent": "aria",
        "steps": [
            {
                "step": 1,
                "code": "GS-01",
                "description": "Read orders",
                "params": {"url": REAL_SHEET},
            }
        ],
    }
    regenerated = {
        "supported": True,
        "title": "Picklist",
        "agent": "aria",
        "steps": [
            {
                "step": 1,
                "code": "GS-01",
                "description": "Read orders",
                "params": {
                    "url": "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit",
                    "spreadsheet_id": "YOUR_SHEET_ID",
                },
            },
            {
                "step": 2,
                "code": "XF-01",
                "description": "Filter paid orders",
                "params": {"status_column": "Financial Status", "status_value": "paid"},
            },
        ],
    }
    fixed = inherit_sheet_bindings(regenerated, prior)
    p1 = fixed["steps"][0]["params"]
    assert "YOUR_SHEET_ID" not in str(p1)
    assert p1["spreadsheet_id"] == "1qlOS1W3p1MIeZKViZEdCLnhXa_8fInsxjGN4Orzo0Ng"
    assert "1qlOS1W3p1MIeZKViZEdCLnhXa_8fInsxjGN4Orzo0Ng" in p1["url"]


def test_prepare_inherits_prior_plan_sheet_and_rejects_bare_placeholder():
    prior = {
        "supported": True,
        "title": "Picklist",
        "agent": "aria",
        "steps": [
            {
                "step": 1,
                "code": "GS-01",
                "description": "Read",
                "params": {"url": REAL_SHEET},
            }
        ],
    }
    bad = {
        "supported": True,
        "title": "Picklist",
        "agent": "aria",
        "steps": [
            {
                "step": 1,
                "code": "GS-01",
                "description": "Read",
                "params": {"url": "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit"},
            }
        ],
    }
    raw = "Updated filter.\n\n```json\n" + __import__("json").dumps(bad) + "\n```"
    content, plan, err = prepare_blueprint_response(raw, prior_plan=prior)
    assert err is None
    assert plan is not None
    assert "YOUR_SHEET_ID" not in str(plan["steps"][0]["params"])
    assert plan["steps"][0]["params"]["spreadsheet_id"].startswith("1qlOS")


def test_validate_sheet_bindings_rejects_placeholder_for_deploy():
    steps = [
        {
            "step": 1,
            "code": "GS-01",
            "params": {"url": "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit"},
        }
    ]
    err = validate_sheet_bindings(steps)
    assert err is not None
    assert "YOUR_SHEET_ID" in err or "placeholder" in err.lower()
