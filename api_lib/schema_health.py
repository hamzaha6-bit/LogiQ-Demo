"""Probe required PostgREST tables so missing migrations fail loudly on /api/health."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from supabase_rest import rest_get_with_error

REQUIRED_TABLES = (
    "clients",
    "client_members",
    "entitlements",
    "client_usage",
    "user_profiles",
    "user_integrations",
    "workflows",
    "workflow_approvals",
    "workflow_runs",
    "blueprint_conversations",
    "blueprint_messages",
    "sheet_connections",
    "schema_migrations",
)


def _table_missing(status: int, body: str) -> bool:
    if status in (0, 404):
        return True
    lower = (body or "").lower()
    return "pgrst205" in lower or "does not exist" in lower or "schema cache" in lower


def check_schema_health() -> Tuple[bool, Dict[str, Any]]:
    missing: List[str] = []
    errors: Dict[str, str] = {}
    for table in REQUIRED_TABLES:
        _rows, status, body = rest_get_with_error(
            table, {"select": "*", "limit": "1"}
        )
        if _table_missing(status, body):
            missing.append(table)
            if body:
                errors[table] = body[:200]
    ok = not missing
    payload: Dict[str, Any] = {
        "ok": ok,
        "missing_tables": missing,
        "checked": list(REQUIRED_TABLES),
    }
    if errors:
        payload["errors"] = errors
    return ok, payload
