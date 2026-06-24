"""Core app routes: ping, health, config, billing, audit, workflow controls."""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys
from urllib.parse import urlparse

_API_DIR = os.path.dirname(os.path.abspath(__file__))
_API_LIB = os.path.normpath(os.path.join(_API_DIR, "..", "api_lib"))
if _API_LIB not in sys.path:
    sys.path.insert(0, _API_LIB)

from billing_checkout import CheckoutError, process_checkout
from http_auth import resolve_user_id
from supabase_rest import pause_workflows_for_user


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")

        if path.endswith("/ping"):
            self._json(200, {"status": "ok", "version": "2"})
        elif path.endswith("/health"):
            self._json(
                200,
                {
                    "status": "ok",
                    "anthropic_configured": bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip()),
                    "supabase_configured": bool(os.environ.get("SUPABASE_URL")),
                },
            )
        elif path.endswith("/config"):
            self._json(
                200,
                {
                    "supabase_url": os.environ.get("SUPABASE_URL", ""),
                    "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY", ""),
                    "supabase_configured": bool(os.environ.get("SUPABASE_URL")),
                    "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
                    "stripe_configured": bool((os.environ.get("STRIPE_SECRET_KEY") or "").strip()),
                },
            )
        elif path.endswith("/billing/status"):
            self._json(
                200,
                {
                    "plan": "starter",
                    "plan_name": "Starter",
                    "usage": {
                        "api_calls": 0,
                        "emails_sent": 0,
                        "api_calls_today": 0,
                        "emails_sent_today": 0,
                        "actions_this_month": 0,
                    },
                    "limits": {},
                    "percentages": {"api_calls": 0, "emails": 0, "actions": 0},
                    "stripe_configured": bool((os.environ.get("STRIPE_SECRET_KEY") or "").strip()),
                },
            )
        elif path.endswith("/audit/log"):
            self._json(200, {"logs": [], "entries": []})
        else:
            self._json(404, {"detail": f"Unknown route: {path}"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if path.endswith("/workflows/emergency-stop"):
            self._emergency_stop_workflows()
        elif path.endswith("/billing/checkout"):
            self._billing_checkout()
        else:
            self._json(404, {"detail": f"Unknown route: {path}"})

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _billing_checkout(self):
        user_id = resolve_user_id(self)
        body = self._read_json_body()
        try:
            result = process_checkout(user_id, body.get("tier"))
            self._json(200, result)
        except CheckoutError as exc:
            self._json(exc.status, {"detail": exc.detail})

    def _emergency_stop_workflows(self):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Valid Bearer token required"})
            return

        paused_count, err = pause_workflows_for_user(user_id, active_only=True)
        if err:
            self._json(502, {"detail": err})
            return

        self._json(200, {"status": "ok", "paused_count": paused_count})

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
