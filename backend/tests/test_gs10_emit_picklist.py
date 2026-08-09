"""Pound Fabrics piece 4: GS-10 emit-picklist (volume balance + exceptions)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "api_lib"))

from picklist_emit import (  # noqa: E402
    EmitError,
    balance_rows,
    build_partitions,
    is_managed_picklist_title,
    parse_emit_params,
    split_exceptions,
)
from workflow_runner import StepExecutionError, _execute_step  # noqa: E402

SHEET_URL = "https://docs.google.com/spreadsheets/d/abc123/edit"
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pound_fabrics" / "shopify_orders.json"


@pytest.fixture
def fixture_rows():
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    columns = ["Name", "Lineitem sku", "Lineitem quantity", "Lineitem name"]
    rows = [{c: r.get(c, "") for c in columns} for r in data["rows"]]
    return {"rows": rows, "columns": columns}


def _sheets_meta(*titles: str):
    return {
        "sheets": [
            {"properties": {"sheetId": i + 1, "title": t}} for i, t in enumerate(titles)
        ]
    }


def _mock_service(*, titles=("Sheet1",)):
    service = MagicMock()
    spreadsheets = service.spreadsheets.return_value
    # Mutable meta so create/delete can be simulated via side effects if needed.
    state = {"titles": list(titles)}

    def _get(**_kwargs):
        m = MagicMock()
        m.execute.return_value = _sheets_meta(*state["titles"])
        return m

    def _batch_update(**kwargs):
        body = kwargs.get("body") or {}
        for req in body.get("requests") or []:
            if "addSheet" in req:
                title = req["addSheet"]["properties"]["title"]
                state["titles"].append(title)
            if "deleteSheet" in req:
                sid = req["deleteSheet"]["sheetId"]
                # sheetId is 1-based index in our meta helper
                idx = sid - 1
                if 0 <= idx < len(state["titles"]):
                    state["titles"].pop(idx)
        m = MagicMock()
        # addSheet reply
        replies = []
        for req in body.get("requests") or []:
            if "addSheet" in req:
                title = req["addSheet"]["properties"]["title"]
                replies.append(
                    {"addSheet": {"properties": {"sheetId": len(state["titles"]), "title": title}}}
                )
            else:
                replies.append({})
        m.execute.return_value = {"replies": replies}
        return m

    spreadsheets.get.side_effect = lambda **kw: _get(**kw)
    spreadsheets.batchUpdate.side_effect = lambda **kw: _batch_update(**kw)
    values_api = spreadsheets.values.return_value
    values_api.update.return_value.execute.return_value = {
        "updatedRange": "A1:D2",
        "updatedRows": 2,
        "updatedCells": 8,
    }
    values_api.clear.return_value.execute.return_value = {"clearedRange": "A:ZZ"}
    return service, spreadsheets, state


# ── Pure partitioning ─────────────────────────────────────────────────────────

def test_exception_routing_blank_whitespace_absent(fixture_rows):
    rows = list(fixture_rows["rows"][:3])
    rows[0]["Lineitem sku"] = "CP-NAVY"
    rows[1]["Lineitem sku"] = "   "  # whitespace
    rows[2] = {k: v for k, v in rows[2].items() if k != "Lineitem sku"}  # absent
    good, bad = split_exceptions(rows, "Lineitem sku")
    assert len(good) == 1
    assert len(bad) == 2


def test_target_rows_per_tab_derives_n(fixture_rows):
    # Take a stretch of fixture rows with SKUs present.
    rows = [r for r in fixture_rows["rows"] if str(r.get("Lineitem sku") or "").strip()][:10]
    plan = build_partitions(
        rows,
        fixture_rows["columns"],
        exception_field="Lineitem sku",
        target_rows_per_tab=4,
        keep_groups_intact=False,
    )
    assert plan["exception_row_count"] == 0
    assert plan["tab_count"] == 3  # ceil(10/4)
    assert [p["row_count"] for p in plan["picklists"]] == [4, 3, 3]


def test_keep_groups_intact_true_does_not_split_order(fixture_rows):
    rows = [r for r in fixture_rows["rows"] if str(r.get("Lineitem sku") or "").strip()]
    # Ensure we have multi-row orders.
    plan = build_partitions(
        rows,
        fixture_rows["columns"],
        exception_field="Lineitem sku",
        target_rows_per_tab=3,
        keep_groups_intact=True,
        group_column="Name",
    )
    for part in plan["picklists"]:
        names = [r["Name"] for r in part["rows"]]
        # Contiguous: once an order leaves, it must not reappear.
        seen = set()
        prev = None
        for n in names:
            if n != prev:
                assert n not in seen, f"order {n} split across non-contiguous segments in {part['sheet_name']}"
                seen.add(n)
            prev = n
        # No order appears on multiple tabs.
    all_names_per_tab = [set(r["Name"] for r in p["rows"]) for p in plan["picklists"]]
    for i, a in enumerate(all_names_per_tab):
        for j, b in enumerate(all_names_per_tab):
            if i >= j:
                continue
            assert a.isdisjoint(b), f"order shared across {plan['picklists'][i]['sheet_name']} and {plan['picklists'][j]['sheet_name']}"


def test_keep_groups_intact_false_allows_even_split():
    rows = [{"Name": f"#{i}", "Lineitem sku": f"S{i}"} for i in range(6)]
    chunks = balance_rows(
        rows,
        target_rows_per_tab=2,
        keep_groups_intact=False,
    )
    assert len(chunks) == 3
    assert [len(c) for c in chunks] == [2, 2, 2]


def test_tab_count_override():
    rows = [{"Name": "A", "Lineitem sku": "1"}, {"Name": "B", "Lineitem sku": "2"},
            {"Name": "C", "Lineitem sku": "3"}, {"Name": "D", "Lineitem sku": "4"}]
    plan = build_partitions(
        rows,
        ["Name", "Lineitem sku"],
        exception_field="Lineitem sku",
        target_rows_per_tab=2,
        tab_count=1,
        keep_groups_intact=False,
    )
    assert plan["tab_count"] == 1
    assert plan["picklists"][0]["row_count"] == 4


def test_empty_input_plan():
    plan = build_partitions(
        [],
        ["Name", "Lineitem sku"],
        exception_field="Lineitem sku",
        target_rows_per_tab=10,
        keep_groups_intact=True,
        group_column="Name",
    )
    assert plan["tab_count"] == 0
    assert plan["picklists"] == []
    assert plan["exceptions"] is None
    assert plan["exception_row_count"] == 0


def test_managed_title_helpers():
    assert is_managed_picklist_title("Picklist 1")
    assert is_managed_picklist_title("Picklist 12")
    assert not is_managed_picklist_title("Picklist")
    assert not is_managed_picklist_title("Orders")
    assert is_managed_picklist_title("PF 2", prefix="PF")


def test_keep_groups_requires_group_column():
    with pytest.raises(EmitError, match="group_column"):
        balance_rows(
            [{"Name": "A", "Lineitem sku": "1"}],
            target_rows_per_tab=5,
            keep_groups_intact=True,
            group_column=None,
        )


# ── GS-10 executor (mocked Sheets) ───────────────────────────────────────────

def test_gs10_emits_exceptions_and_picklists(fixture_rows):
    rows = list(fixture_rows["rows"][:6])
    # Force two exceptions.
    rows[0]["Lineitem sku"] = ""
    rows[1]["Lineitem sku"] = "  "
    service, spreadsheets, state = _mock_service(titles=("Sheet1",))
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = _execute_step(
            "GS-10",
            {
                "url": SHEET_URL,
                "rows": rows,
                "columns": fixture_rows["columns"],
                "exception_field": "Lineitem sku",
                "target_rows_per_tab": 10,
                "keep_groups_intact": True,
                "group_column": "Name",
            },
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert out["success"] is True
    assert out["exception_row_count"] == 2
    assert out["exception_tab"]["sheet_name"] == "Exceptions"
    assert out["exception_tab"]["row_count"] == 2
    assert out["tab_count"] >= 1
    assert any(t["sheet_name"] == "Picklist 1" for t in out["picklist_tabs"])
    # createSheet + writes happened
    assert spreadsheets.batchUpdate.called
    assert spreadsheets.values.return_value.update.called


def test_gs10_idempotent_rerun_deletes_orphan_picklist_tabs(fixture_rows):
    rows = [r for r in fixture_rows["rows"] if str(r.get("Lineitem sku") or "").strip()][:4]
    service, spreadsheets, state = _mock_service(
        titles=("Sheet1", "Picklist 1", "Picklist 2", "Picklist 3", "Exceptions")
    )
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = _execute_step(
            "GS-10",
            {
                "url": SHEET_URL,
                "rows": rows,
                "columns": fixture_rows["columns"],
                "exception_field": "Lineitem sku",
                "tab_count": 1,  # only need Picklist 1 now
                "keep_groups_intact": False,
            },
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert out["tab_count"] == 1
    assert out["deleted_orphan_tabs"] >= 1
    # Orphans Picklist 2/3 and possibly Exceptions (no exceptions this run) removed.
    delete_reqs = []
    for call in spreadsheets.batchUpdate.call_args_list:
        body = call.kwargs.get("body") or {}
        for req in body.get("requests") or []:
            if "deleteSheet" in req:
                delete_reqs.append(req)
    assert delete_reqs, "expected deleteSheet for orphan picklist tabs"


def test_gs10_empty_input_cleans_managed_tabs():
    service, spreadsheets, state = _mock_service(
        titles=("Sheet1", "Picklist 1", "Exceptions")
    )
    with patch("sheets_service._require_sheets"), \
         patch("sheets_service.get_sheets_service", return_value=service):
        out = _execute_step(
            "GS-10",
            {
                "url": SHEET_URL,
                "rows": [],
                "columns": ["Name", "Lineitem sku"],
                "exception_field": "Lineitem sku",
                "target_rows_per_tab": 5,
                "keep_groups_intact": True,
                "group_column": "Name",
            },
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
    assert out["success"] is True
    assert out["tab_count"] == 0
    assert out["tabs"] == []
    assert out["deleted_orphan_tabs"] >= 1


def test_gs10_preserves_group_boundaries_metadata():
    rows = [
        {"Name": "#1", "Lineitem sku": "A"},
        {"Name": "#1", "Lineitem sku": "B"},
        {"Name": "#2", "Lineitem sku": "C"},
    ]
    plan = parse_emit_params(
        {
            "rows": rows,
            "columns": ["Name", "Lineitem sku"],
            "exception_field": "Lineitem sku",
            "target_rows_per_tab": 50,
            "keep_groups_intact": True,
            "group_column": "Name",
        }
    )
    assert plan["picklists"][0]["group_boundaries"] == [0, 2]


def test_gs10_requires_url():
    with pytest.raises(StepExecutionError, match="url|spreadsheet"):
        _execute_step(
            "GS-10",
            {
                "rows": [],
                "columns": ["Name"],
                "exception_field": "Name",
                "target_rows_per_tab": 5,
            },
            user_id="u1",
            agent_id="aria",
            agent_name="Aria",
        )
