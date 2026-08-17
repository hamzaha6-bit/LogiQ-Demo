"""F1 — schema health flags missing tables."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from schema_health import REQUIRED_TABLES, check_schema_health  # noqa: E402


@patch("schema_health.rest_get_with_error")
def test_schema_health_reports_missing_tables(mock_get):
    def _fake(table, params=None):
        if table == "blueprint_messages":
            return [], 404, "PGRST205"
        return [{"id": "x"}], 200, ""

    mock_get.side_effect = _fake
    ok, payload = check_schema_health()
    assert ok is False
    assert "blueprint_messages" in payload["missing_tables"]
    assert set(payload["checked"]) == set(REQUIRED_TABLES)
