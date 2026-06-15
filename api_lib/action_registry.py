"""LogiQ action primitive registry — Gmail (GM), Google Sheets (GS), Google Calendar (GC)."""

from typing import Any, Dict, List, Optional

ACTION_REGISTRY: Dict[str, Dict[str, Any]] = {
    "GM-01": {"integration": "Gmail", "name": "List messages", "requires_approval": False},
    "GM-02": {"integration": "Gmail", "name": "Read message", "requires_approval": False},
    "GM-03": {"integration": "Gmail", "name": "Send email", "requires_approval": True},
    "GM-04": {"integration": "Gmail", "name": "Reply to thread", "requires_approval": True},
    "GM-05": {"integration": "Gmail", "name": "Draft email", "requires_approval": False},
    "GM-06": {"integration": "Gmail", "name": "Label message", "requires_approval": False},
    "GM-07": {"integration": "Gmail", "name": "Search inbox", "requires_approval": False},
    "GM-08": {"integration": "Gmail", "name": "Get thread", "requires_approval": False},
    "GS-01": {"integration": "Google Sheets", "name": "Read sheet", "requires_approval": False},
    "GS-02": {"integration": "Google Sheets", "name": "Append row", "requires_approval": False},
    "GS-03": {"integration": "Google Sheets", "name": "Update row", "requires_approval": False},
    "GS-04": {"integration": "Google Sheets", "name": "Poll for new rows", "requires_approval": False},
    "GS-05": {"integration": "Google Sheets", "name": "Connect sheet", "requires_approval": False},
    "GS-06": {"integration": "Google Sheets", "name": "Delete row", "requires_approval": True},
    "GS-07": {"integration": "Google Sheets", "name": "Write cell", "requires_approval": False},
    "GC-01": {"integration": "Google Calendar", "name": "Check availability", "requires_approval": False},
    "GC-02": {"integration": "Google Calendar", "name": "List events", "requires_approval": False},
    "GC-03": {"integration": "Google Calendar", "name": "Create event", "requires_approval": False},
    "GC-04": {"integration": "Google Calendar", "name": "Update event", "requires_approval": False},
    "GC-05": {"integration": "Google Calendar", "name": "Cancel event", "requires_approval": True},
    "GC-06": {"integration": "Google Calendar", "name": "Send calendar invite", "requires_approval": True},
}

IRREVERSIBLE_CODES = frozenset({"GM-03", "GM-04", "GC-05", "GC-06", "GS-06"})


def registry_for_prompt() -> List[Dict[str, Any]]:
    return [
        {
            "code": code,
            "integration": meta["integration"],
            "name": meta["name"],
            "requires_approval": meta["requires_approval"],
        }
        for code, meta in ACTION_REGISTRY.items()
    ]


def validate_plan_steps(steps: List[Dict[str, Any]]) -> Optional[str]:
    if not steps:
        return "Workflow must include at least one step"
    for i, step in enumerate(steps, start=1):
        code = (step.get("code") or "").strip().upper()
        if code not in ACTION_REGISTRY:
            return f"Step {i}: unknown primitive {code!r}"
        meta = ACTION_REGISTRY[code]
        if meta["requires_approval"] and not step.get("requires_approval"):
            step["requires_approval"] = True
    return None
