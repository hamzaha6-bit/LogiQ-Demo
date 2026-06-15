"""Core app routes: ping, health, config, billing, audit."""
from http.server import BaseHTTPRequestHandler
import json
import os
from urllib.parse import urlparse


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
                    "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
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
                    "stripe_configured": False,
                },
            )
        elif path.endswith("/audit/log"):
            self._json(200, {"logs": [], "entries": []})
        else:
            self._json(404, {"detail": f"Unknown route: {path}"})

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
