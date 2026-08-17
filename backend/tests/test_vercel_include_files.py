"""G12 — Vercel includeFiles must ship picklist/crypto modules."""

from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent


def test_app_and_ai_include_picklist_and_crypto():
    data = json.loads((_ROOT / "vercel.json").read_text(encoding="utf-8"))
    by_src = {}
    for build in data["builds"]:
        src = build.get("src")
        files = (build.get("config") or {}).get("includeFiles") or ""
        by_src[src] = files
    app = by_src["api/app.py"]
    ai = by_src["api/ai.py"]
    for needed in (
        "api_lib/crypto.py",
        "api_lib/picklist_emit.py",
        "api_lib/picklist_format.py",
        "api_lib/sheet_transforms.py",
        "api_lib/schema_health.py",
        "api_lib/workflow_approvals.py",
        "api_lib/billing_auth.py",
    ):
        assert needed in app, needed
    for needed in ("api_lib/crypto.py", "api_lib/sheets_service.py", "api_lib/google_oauth.py"):
        assert needed in ai, needed
