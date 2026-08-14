"""Pound Fabrics piece 1: XF-01..XF-05 in-memory transforms."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "api_lib"))

from sheet_transforms import (  # noqa: E402
    TransformError,
    aggregate_rows,
    derive_columns,
    drop_groups,
    execute_transform,
    filter_rows,
    project_columns,
    sort_rows,
)
from workflow_context import empty_context, resolve_params, set_step_output  # noqa: E402
from workflow_runner import StepExecutionError, _execute_step  # noqa: E402

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pound_fabrics" / "shopify_orders.json"


@pytest.fixture
def shopify_table():
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return {"rows": data["rows"], "columns": data["columns"]}


def _orders(rows):
    return {r["Name"] for r in rows}


# ── XF-01 Filter ─────────────────────────────────────────────────────────────

def test_xf01_filter_status_and_id_range(shopify_table):
    out = filter_rows(
        shopify_table["rows"],
        shopify_table["columns"],
        status_column="Financial Status",
        status_value="paid",
        id_column="Id",
        min_id=501001,
        max_id=501004,
    )
    assert out["success"] is True
    # #1001 (3), #1002 (2), #1004 (5) — unpaid #1003 excluded; #1005 id too high; non-numeric #1006 excluded
    assert out["row_count"] == 10
    assert _orders(out["rows"]) == {"#1001", "#1002", "#1004"}
    assert all(r["Financial Status"].lower() == "paid" for r in out["rows"])


def test_xf01_non_numeric_id_excluded(shopify_table):
    out = execute_transform(
        "XF-01",
        {
            **shopify_table,
            "status_column": "Financial Status",
            "status_value": "Paid",  # case-insensitive
            "id_column": "Id",
            "min_id": 501006,
            "max_id": 501006,
        },
    )
    # #1006 is paid but Id="not-a-number"
    assert out["row_count"] == 0


def test_xf01_status_case_insensitive(shopify_table):
    out = execute_transform(
        "XF-01",
        {
            **shopify_table,
            "status_value": "PAID",
            "id_column": "Id",
            "min_id": 501005,
            "max_id": 501005,
        },
    )
    assert out["row_count"] == 2
    assert _orders(out["rows"]) == {"#1005"}


def test_xf01_shopify_order_name_hash_prefix_range():
    """SOP 10–13: bound by order Name (#694358), not Shopify internal Id."""
    rows = [
        {"Name": "#694100", "Financial Status": "paid", "sku": "A"},
        {"Name": "#694550", "Financial Status": "paid", "sku": "B"},
        {"Name": "#694580", "Financial Status": "paid", "sku": "C"},
        {"Name": "#694900", "Financial Status": "paid", "sku": "D"},
        {"Name": "#694550", "Financial Status": "paid", "sku": "B2"},
    ]
    columns = ["Name", "Financial Status", "sku"]
    out = filter_rows(
        rows,
        columns,
        status_column="Financial Status",
        status_value="paid",
        id_column="Name",
        min_id=694550,
        max_id=694580,
    )
    assert _orders(out["rows"]) == {"#694550", "#694580"}
    assert out["row_count"] == 3  # both lines of #694550 kept


def test_xf01_order_name_accepts_hash_in_bounds():
    rows = [
        {"Name": "#694550", "Financial Status": "paid"},
        {"Name": "694560", "Financial Status": "paid"},
    ]
    out = execute_transform(
        "XF-01",
        {
            "rows": rows,
            "columns": ["Name", "Financial Status"],
            "id_column": "Name",
            "min_id": "#694550",
            "max_id": "#694560",
        },
    )
    assert out["row_count"] == 2


# ── XF-02 Group-aware drop ───────────────────────────────────────────────────

def test_xf02_drops_entire_order_on_express_line(shopify_table):
    out = drop_groups(
        shopify_table["rows"],
        shopify_table["columns"],
        group_column="Name",
        match_column="Lineitem name",
        op="contains",
        value="Express Shipping",
    )
    # #1002 has an Express Shipping lineitem → both lines gone
    assert "#1002" not in _orders(out["rows"])
    assert "#1001" in _orders(out["rows"])
    assert out["dropped_group_count"] >= 1
    assert "group_boundaries" in out


def test_xf02_mixed_shipping_methods_same_order(shopify_table):
    """#1005 has Standard on one line and Royal Mail on another — keep whole order."""
    out = execute_transform(
        "XF-02",
        {
            **shopify_table,
            "group_column": "Name",
            "condition": {
                "column": "Lineitem name",
                "op": "contains",
                "value": "express shipping",  # case-insensitive
            },
        },
    )
    assert "#1005" in _orders(out["rows"])
    assert len([r for r in out["rows"] if r["Name"] == "#1005"]) == 2


def test_xf02_blank_keys_are_singletons(shopify_table):
    """Matching Express on one blank-Name row must not drop the other blank-Name row."""
    out = drop_groups(
        shopify_table["rows"],
        shopify_table["columns"],
        group_column="Name",
        match_column="Lineitem name",
        op="contains",
        value="Express Shipping",
    )
    blank_rows = [r for r in out["rows"] if r["Name"] == ""]
    assert len(blank_rows) == 1
    assert blank_rows[0]["Email"] == "other-blank@example.com"


def test_xf02_equals_op(shopify_table):
    out = execute_transform(
        "XF-02",
        {
            **shopify_table,
            "group_column": "Name",
            "match_column": "Shipping Method",
            "op": "equals",
            "value": "Express Shipping",
        },
    )
    assert "#1002" not in _orders(out["rows"])


# ── XF-03 Column subset ──────────────────────────────────────────────────────

def test_xf03_project_columns(shopify_table):
    keep = ["Name", "Lineitem sku", "Lineitem quantity", "Financial Status"]
    out = project_columns(shopify_table["rows"], shopify_table["columns"], keep=keep)
    assert out["columns"] == keep
    assert all(set(r.keys()) == set(keep) for r in out["rows"])
    assert out["row_count"] == len(shopify_table["rows"])


def test_xf03_missing_column_fails(shopify_table):
    with pytest.raises(TransformError, match="Missing column"):
        project_columns(
            shopify_table["rows"],
            shopify_table["columns"],
            keep=["Name", "No Such Column"],
        )


# ── XF-04 Sort ───────────────────────────────────────────────────────────────

def test_xf04_sort_primary_secondary(shopify_table):
    out = sort_rows(
        shopify_table["rows"],
        shopify_table["columns"],
        primary="Lineitem sku",
        secondary="Lineitem quantity",
    )
    skus = [(r["Lineitem sku"], r["Lineitem quantity"]) for r in out["rows"]]
    assert skus == sorted(skus, key=lambda t: (t[0].casefold(), float(t[1]) if t[1].isdigit() else t[1]))
    assert out["sorted_by"] == ["Lineitem sku", "Lineitem quantity"]
    assert out["group_boundaries"][0] == 0


# ── XF-05 Aggregate ──────────────────────────────────────────────────────────

def test_xf05_aggregate_format_string(shopify_table):
    # Use filtered paid navy 3m lines: #1001 has two CP-NAVY@3, #1002 one, #1006 one (non-numeric id irrelevant here)
    navy3 = [
        r
        for r in shopify_table["rows"]
        if r["Lineitem sku"] == "CP-NAVY" and r["Lineitem quantity"] == "3"
    ]
    out = aggregate_rows(
        navy3,
        shopify_table["columns"],
        sku_column="Lineitem sku",
        qty_column="Lineitem quantity",
        format_string="{qty}m x {count}",
        summary_column="Pick Summary",
    )
    assert out["row_count"] == 1
    assert out["rows"][0]["Pick Summary"] == "3m x 5"
    assert out["group_sizes"] == [5]
    assert out["format_string"] == "{qty}m x {count}"


def test_xf05_format_must_be_param(shopify_table):
    with pytest.raises(TransformError, match="format_string"):
        execute_transform("XF-05", {**shopify_table, "sku_column": "Lineitem sku", "qty_column": "Lineitem quantity"})


def test_xf05_min_count_4_leaves_triples_unmerged():
    """Pound Fabrics SOP: merge only when same SKU AND same qty appear 4+ times."""
    rows = (
        [{"Name": "#1", "Lineitem sku": "AAA", "Lineitem quantity": "3"}] * 3
        + [{"Name": "#2", "Lineitem sku": "AAA", "Lineitem quantity": "3"}] * 2
        + [{"Name": "#3", "Lineitem sku": "BBB", "Lineitem quantity": "2"}] * 3
        + [{"Name": "#4", "Lineitem sku": "CCC", "Lineitem quantity": "5"}]
    )
    # AAA@3 appears 5 times → merge; BBB@2 appears 3 → unmerged; CCC@5 once → unmerged
    out = aggregate_rows(
        rows,
        ["Name", "Lineitem sku", "Lineitem quantity"],
        sku_column="Lineitem sku",
        qty_column="Lineitem quantity",
        format_string="{qty}m x {count}",
        min_count=4,
        write_summary_to_qty=True,
        preserve_other_columns=True,
    )
    aaa = [r for r in out["rows"] if r["Lineitem sku"] == "AAA"]
    bbb = [r for r in out["rows"] if r["Lineitem sku"] == "BBB"]
    ccc = [r for r in out["rows"] if r["Lineitem sku"] == "CCC"]
    assert len(aaa) == 1
    assert aaa[0]["Lineitem quantity"] == "3m x 5"
    assert len(bbb) == 3
    assert all(r["Lineitem quantity"] == "2" for r in bbb)
    assert len(ccc) == 1
    assert ccc[0]["Lineitem quantity"] == "5"
    assert out["merged_group_count"] == 1


def test_xf05_literal_sop_four_plus_same_sku_qty():
    """Documented SOP 37: 4+ identical SKU+qty → one cell; below 4 stay as rows.

    Spec is the client SOP, not a historical Picklist-Completed.xlsm (step 15
    already-printed / edits are not visible in a static export).
    """
    rows = (
        [{"Lineitem sku": "CUT-3", "Lineitem quantity": "3"}] * 5
        + [{"Lineitem sku": "CUT-3", "Lineitem quantity": "2"}] * 3
    )
    out = aggregate_rows(
        rows,
        ["Lineitem sku", "Lineitem quantity"],
        sku_column="Lineitem sku",
        qty_column="Lineitem quantity",
        format_string="{qty}m x {count}",
        min_count=4,
        write_summary_to_qty=True,
        preserve_other_columns=True,
    )
    merged = [r for r in out["rows"] if r["Lineitem quantity"] == "3m x 5"]
    unmerged = [r for r in out["rows"] if r["Lineitem quantity"] == "2"]
    assert len(merged) == 1
    assert len(unmerged) == 3
    assert out["row_count"] == 4
    assert out["merged_group_count"] == 1


def test_xf05_does_not_merge_same_sku_different_qty():
    rows = (
        [{"Lineitem sku": "AAA", "Lineitem quantity": "3"}] * 4
        + [{"Lineitem sku": "AAA", "Lineitem quantity": "5"}] * 4
    )
    out = aggregate_rows(
        rows,
        ["Lineitem sku", "Lineitem quantity"],
        sku_column="Lineitem sku",
        qty_column="Lineitem quantity",
        format_string="{qty}m x {count}",
        min_count=4,
        preserve_other_columns=False,
    )
    summaries = {(r["Lineitem sku"], r["Lineitem quantity"]): r["Summary"] for r in out["rows"]}
    assert summaries[("AAA", "3")] == "3m x 4"
    assert summaries[("AAA", "5")] == "5m x 4"
    assert out["row_count"] == 2


def test_xf05_multiple_groups(shopify_table):
    out = execute_transform(
        "XF-05",
        {
            **shopify_table,
            "sku_column": "Lineitem sku",
            "qty_column": "Lineitem quantity",
            "format_string": "{qty}m x {count}",
            "preserve_other_columns": False,
        },
    )
    summaries = { (r["Lineitem sku"], r["Lineitem quantity"]): r["Summary"] for r in out["rows"] }
    assert summaries[("CP-NAVY", "3")] == "3m x 5"
    assert summaries[("VEL-EM", "5")] == "5m x 5"
    assert summaries[("LB-NAT", "2")] == "2m x 2"


# ── Runner wiring + template object passthrough ───────────────────────────────

def test_execute_step_xf01_via_runner(shopify_table):
    out = _execute_step(
        "XF-01",
        {
            **shopify_table,
            "status_value": "paid",
            "id_column": "Id",
            "min_id": 501001,
            "max_id": 501001,
        },
        user_id="u1",
        agent_id="aria",
        agent_name="Aria",
    )
    assert out["row_count"] == 3
    assert all(r["Name"] == "#1001" for r in out["rows"])


def test_resolve_params_preserves_rows_object():
    ctx = empty_context()
    rows = [{"a": 1}, {"a": 2}]
    set_step_output(ctx, 1, {"rows": rows, "columns": ["a"]})
    resolved = resolve_params(
        {"rows": "{{step_1.output.rows}}", "columns": "{{step_1.output.columns}}"},
        ctx,
    )
    assert resolved["rows"] == rows
    assert resolved["columns"] == ["a"]
    assert isinstance(resolved["rows"], list)


def test_execute_step_rejects_bad_xf_params():
    with pytest.raises(StepExecutionError, match="rows"):
        _execute_step("XF-03", {"keep": ["Name"]}, user_id="u1", agent_id="aria", agent_name="Aria")


# ── Pipeline smoke (Shopify shape) ───────────────────────────────────────────

def test_shopify_pipeline_filter_drop_project_sort_aggregate(shopify_table):
    filtered = execute_transform(
        "XF-01",
        {
            **shopify_table,
            "status_value": "paid",
            "id_column": "Id",
            "min_id": 501001,
            "max_id": 501010,
        },
    )
    dropped = execute_transform(
        "XF-02",
        {
            "rows": filtered["rows"],
            "columns": filtered["columns"],
            "group_column": "Name",
            "match_column": "Lineitem name",
            "op": "contains",
            "value": "Express Shipping",
        },
    )
    assert "#1002" not in _orders(dropped["rows"])

    projected = execute_transform(
        "XF-03",
        {
            "rows": dropped["rows"],
            "columns": dropped["columns"],
            "keep": ["Name", "Lineitem sku", "Lineitem quantity", "Id"],
        },
    )
    sorted_out = execute_transform(
        "XF-04",
        {
            "rows": projected["rows"],
            "columns": projected["columns"],
            "primary": "Lineitem sku",
            "secondary": "Lineitem quantity",
            "group_column": "Name",
        },
    )
    aggregated = execute_transform(
        "XF-05",
        {
            "rows": sorted_out["rows"],
            "columns": sorted_out["columns"],
            "sku_column": "Lineitem sku",
            "qty_column": "Lineitem quantity",
            "format_string": "{qty}m x {count}",
            "preserve_other_columns": False,
        },
    )
    assert aggregated["success"] is True
    assert aggregated["row_count"] >= 1
    assert any(r["Summary"].endswith("x 5") or "m x" in r["Summary"] for r in aggregated["rows"])


# ── XF-06 Ordered derive + optional filter ───────────────────────────────────

_CREDIT_COLUMNS = [
    "Customer",
    "Current balance GBP",
    "Credit limit GBP",
    "Oldest unpaid invoice date",
]

_CREDIT_DERIVE = [
    {
        "column": "Over limit",
        "op": "gt",
        "left_column": "Current balance GBP",
        "right_column": "Credit limit GBP",
    },
    {
        "column": "Days overdue",
        "op": "days_since",
        "date_column": "Oldest unpaid invoice date",
    },
    {
        "column": "Overdue >30 days",
        "op": "gt",
        "left_column": "Days overdue",
        "right_value": 30,
    },
    {
        "column": "Flagged",
        "op": "or",
        "columns": ["Over limit", "Overdue >30 days"],
    },
]


@pytest.fixture
def credit_table():
    # as_of fixed to 2026-08-11 in tests
    rows = [
        {
            "Customer": "OverLimitCo",
            "Current balance GBP": "12000",
            "Credit limit GBP": "10000",
            "Oldest unpaid invoice date": "2026-07-20",  # 22 days — not >30
        },
        {
            "Customer": "OverdueCo",
            "Current balance GBP": "500",
            "Credit limit GBP": "5000",
            "Oldest unpaid invoice date": "2026-06-01",  # 71 days
        },
        {
            "Customer": "BothCo",
            "Current balance GBP": "£8,500.00",
            "Credit limit GBP": "8000",
            "Oldest unpaid invoice date": "01/05/2026",  # 102 days (UK)
        },
        {
            "Customer": "CleanCo",
            "Current balance GBP": "1000",
            "Credit limit GBP": "5000",
            "Oldest unpaid invoice date": "2026-08-01",  # 10 days
        },
    ]
    return {"rows": rows, "columns": list(_CREDIT_COLUMNS)}


def test_xf06_credit_review_ordered_derive_and_filter(credit_table):
    """Flagged must be computed LAST from Over limit OR Overdue >30 — not required on input."""
    assert "Flagged" not in credit_table["columns"]
    out = execute_transform(
        "XF-06",
        {
            **credit_table,
            "derive": _CREDIT_DERIVE,
            "filter_column": "Flagged",
            "filter_value": "yes",
            "as_of": "2026-08-11",
        },
    )
    assert out["success"] is True
    assert "Flagged" in out["columns"]
    assert out["derived_columns"] == [
        "Over limit",
        "Days overdue",
        "Overdue >30 days",
        "Flagged",
    ]
    names = {r["Customer"] for r in out["rows"]}
    assert names == {"OverLimitCo", "OverdueCo", "BothCo"}
    assert "CleanCo" not in names
    by = {r["Customer"]: r for r in out["rows"]}
    assert by["OverLimitCo"]["Over limit"] == "yes"
    assert by["OverLimitCo"]["Overdue >30 days"] == "no"
    assert by["OverLimitCo"]["Flagged"] == "yes"
    assert by["OverdueCo"]["Over limit"] == "no"
    assert by["OverdueCo"]["Days overdue"] == "71"
    assert by["OverdueCo"]["Overdue >30 days"] == "yes"
    assert by["OverdueCo"]["Flagged"] == "yes"
    assert by["BothCo"]["Over limit"] == "yes"
    assert by["BothCo"]["Overdue >30 days"] == "yes"
    assert by["BothCo"]["Flagged"] == "yes"


def test_xf06_does_not_require_flagged_on_input(credit_table):
    out = derive_columns(
        credit_table["rows"],
        credit_table["columns"],
        derive=_CREDIT_DERIVE,
        as_of="2026-08-11",
    )
    assert out["row_count"] == 4
    assert all(r["Flagged"] in ("yes", "no") for r in out["rows"])
    clean = next(r for r in out["rows"] if r["Customer"] == "CleanCo")
    assert clean["Flagged"] == "no"
    assert clean["Days overdue"] == "10"


def test_xf06_missing_source_column_still_errors(credit_table):
    with pytest.raises(TransformError, match="Missing column\\(s\\): Current balance GBP"):
        execute_transform(
            "XF-06",
            {
                "rows": credit_table["rows"],
                "columns": ["Customer", "Credit limit GBP", "Oldest unpaid invoice date"],
                "derive": _CREDIT_DERIVE,
                "as_of": "2026-08-11",
            },
        )


def test_xf06_flagged_as_input_to_xf01_still_missing_without_derive(credit_table):
    """Reproduce the demo failure mode: XF-01 treating Flagged as a required input."""
    with pytest.raises(TransformError, match="Missing column\\(s\\): Flagged"):
        execute_transform(
            "XF-01",
            {
                **credit_table,
                "status_column": "Flagged",
                "status_value": "yes",
                "id_column": "Current balance GBP",
                "min_id": 0,
                "max_id": 999999,
            },
        )


def test_execute_step_xf06_via_runner(credit_table):
    out = _execute_step(
        "XF-06",
        {
            **credit_table,
            "derive": _CREDIT_DERIVE,
            "keep_when": {"column": "Flagged", "op": "equals", "value": "yes"},
            "as_of": "2026-08-11",
        },
        user_id="u1",
        agent_id="nova",
        agent_name="Nova",
    )
    assert out["row_count"] == 3
    assert all(r["Flagged"] == "yes" for r in out["rows"])
