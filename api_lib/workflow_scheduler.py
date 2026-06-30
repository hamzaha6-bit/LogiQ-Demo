"""Workflow schedule parsing and next-run computation (UTC)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional


VALID_FREQ = frozenset({"once", "hourly", "daily", "weekly"})


def parse_schedule(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(str(raw))
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    freq = (data.get("freq") or "").strip().lower()
    if freq not in VALID_FREQ:
        return None
    out: Dict[str, Any] = {"freq": freq}
    if freq in ("daily", "weekly"):
        time_utc = (data.get("time_utc") or "09:00").strip()
        parts = time_utc.split(":")
        if len(parts) != 2:
            return None
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        out["time_utc"] = f"{hour:02d}:{minute:02d}"
    if freq == "weekly":
        weekday = data.get("weekday")
        if weekday is None:
            weekday = 0
        weekday = int(weekday)
        if not (0 <= weekday <= 6):
            return None
        out["weekday"] = weekday
    return out


def schedule_label(schedule: Optional[Dict[str, Any]]) -> str:
    if not schedule:
        return "Run once on deploy"
    freq = schedule.get("freq")
    if freq == "hourly":
        return "Every hour"
    if freq == "daily":
        return f"Daily at {schedule.get('time_utc', '09:00')} UTC"
    if freq == "weekly":
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        wd = int(schedule.get("weekday", 0))
        return f"Weekly on {days[wd]} at {schedule.get('time_utc', '09:00')} UTC"
    if freq == "once":
        return "Run once on deploy"
    return "Run once on deploy"


def compute_next_run(schedule: Optional[Dict[str, Any]], after: Optional[datetime] = None) -> Optional[datetime]:
    """Return the next UTC run time after `after`, or None for one-shot schedules."""
    if not schedule:
        return None
    freq = schedule.get("freq")
    if freq == "once":
        return None
    now = after or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if freq == "hourly":
        return now + timedelta(hours=1)

    time_utc = schedule.get("time_utc", "09:00")
    hour, minute = (int(x) for x in time_utc.split(":"))

    if freq == "daily":
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if freq == "weekly":
        target_weekday = int(schedule.get("weekday", 0))  # 0=Mon
        # Python weekday: Mon=0 … Sun=6
        days_ahead = (target_weekday - now.weekday()) % 7
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    return None


def initial_next_run(schedule: Optional[Dict[str, Any]]) -> Optional[datetime]:
    """First run is immediate on deploy; subsequent runs use compute_next_run."""
    if not schedule or schedule.get("freq") == "once":
        return None
    return datetime.now(timezone.utc)
