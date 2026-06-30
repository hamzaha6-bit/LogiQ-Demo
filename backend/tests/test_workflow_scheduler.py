"""Tests for workflow schedule parsing and next-run computation."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from workflow_scheduler import (  # noqa: E402
    compute_next_run,
    initial_next_run,
    parse_schedule,
    schedule_label,
)


def test_parse_daily_schedule():
    sched = parse_schedule('{"freq": "daily", "time_utc": "09:30"}')
    assert sched == {"freq": "daily", "time_utc": "09:30"}


def test_hourly_next_run():
    now = datetime(2026, 6, 15, 10, 30, tzinfo=timezone.utc)
    sched = parse_schedule({"freq": "hourly"})
    nxt = compute_next_run(sched, now)
    assert nxt > now


def test_initial_next_run_recurring():
    sched = parse_schedule({"freq": "daily", "time_utc": "09:00"})
    nxt = initial_next_run(sched)
    assert nxt is not None


def test_schedule_label_daily():
    label = schedule_label({"freq": "daily", "time_utc": "09:00"})
    assert "Daily" in label
