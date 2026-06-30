"""Tests for workflow context template resolution."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from workflow_context import (  # noqa: E402
    empty_context,
    resolve_params,
    set_step_output,
)


def test_resolve_simple_field():
    ctx = empty_context()
    set_step_output(ctx, 1, {"email": "a@example.com", "invoice_id": "INV-42"})
    resolved = resolve_params(
        {"to": "{{step_1.output.email}}", "body": "Invoice {{step_1.output.invoice_id}} is overdue."},
        ctx,
    )
    assert resolved["to"] == "a@example.com"
    assert resolved["body"] == "Invoice INV-42 is overdue."


def test_unresolved_template_left_as_is():
    ctx = empty_context()
    warnings = []
    resolved = resolve_params({"to": "{{step_9.output.email}}"}, ctx, warnings)
    assert resolved["to"] == "{{step_9.output.email}}"
    assert warnings


def test_resolve_nested_rows():
    ctx = empty_context()
    set_step_output(ctx, 1, {"rows": [{"email": "first@example.com"}]})
    resolved = resolve_params({"to": "{{step_1.output.rows.0.email}}"}, ctx)
    assert resolved["to"] == "first@example.com"
