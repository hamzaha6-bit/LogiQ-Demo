from http.server import BaseHTTPRequestHandler
import json
import os

import anthropic

MODEL = "claude-sonnet-4-5"


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"detail": f"Invalid JSON body: {exc}"})
            return

        api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            self._json(503, {"detail": "ANTHROPIC_API_KEY not configured"})
            return

        system = body.get("system") or ""
        messages = body.get("messages") or []
        max_tokens = int(body.get("max_tokens") or 1200)

        if not messages:
            self._json(400, {"detail": "messages is required"})
            return

        try:
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": m.get("role", "user"), "content": m.get("content", "")} for m in messages],
            )
            content = response.content[0].text if response.content else ""
            self._json(200, {"content": content})
        except anthropic.APIError as exc:
            self._json(502, {"detail": str(exc)})
        except Exception as exc:
            self._json(500, {"detail": str(exc) or "Chat request failed"})

    def _json(self, status, payload):
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
