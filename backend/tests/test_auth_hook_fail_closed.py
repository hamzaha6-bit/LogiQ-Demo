"""Auth hook must fail closed when SUPABASE_AUTH_HOOK_SECRET is missing."""

from __future__ import annotations

import io
import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "api_lib"))

from hook_handler import handle_user_created_hook  # noqa: E402


class _DummyHandler(BaseHTTPRequestHandler):
    def __init__(self):
        self.headers = {"Content-Length": "2"}
        self.rfile = io.BytesIO(b"{}")
        self.wfile = io.BytesIO()
        self.status = None
        self._headers_out = {}

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        self._headers_out[key] = value

    def end_headers(self):
        return

    def log_message(self, format, *args):
        return


def test_missing_hook_secret_returns_401(monkeypatch):
    monkeypatch.delenv("SUPABASE_AUTH_HOOK_SECRET", raising=False)
    handler = _DummyHandler()
    with patch("hook_handler._hook_secret", return_value=""):
        handle_user_created_hook(handler)
    assert handler.status == 401
    payload = json.loads(handler.wfile.getvalue().decode())
    assert "error" in payload
