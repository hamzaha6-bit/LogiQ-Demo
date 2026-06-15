"""Minimal ai.py — stdlib only, for Vercel cold-start diagnostics."""
from http.server import BaseHTTPRequestHandler
import json
import os


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._json(200, {"status": "ok"})

    def do_POST(self):
        self._json(200, {"status": "ok"})

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
