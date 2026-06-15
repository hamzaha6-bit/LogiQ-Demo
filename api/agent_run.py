"""SSE agent pipeline — mirrors backend/main.py agent_run."""
from http.server import BaseHTTPRequestHandler
import json
import os
import re

import anthropic

from http_auth import resolve_user_id

MODEL = "claude-sonnet-4-5"


def _sse_event(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _parse_agent_json(text: str) -> dict:
    text = (text or "").strip()
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {"action": "review", "reasoning": text, "subject": "", "body": text}


def _build_system_prompt(base: str, settings: dict, agent_name: str) -> str:
    parts = [base or f"You are {agent_name}, a LogiQ workflow agent."]
    if settings.get("business"):
        parts.append(f"Business context: {settings['business']}")
    if settings.get("tone"):
        parts.append(f"Tone: {settings['tone']}")
    if settings.get("cta"):
        parts.append(f"Call to action: {settings['cta']}")
    if settings.get("calendly_link"):
        parts.append(f"Calendly link: {settings['calendly_link']}")
    if settings.get("from_name"):
        parts.append(f"Sign emails as: {settings['from_name']}")
    parts.append(
        'Respond with JSON only: {"reasoning":"...","action":"email|wait|none|review","subject":"...","body":"..."}'
    )
    return "\n\n".join(parts)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        user_id = resolve_user_id(self)
        api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            self._json(503, {"detail": "ANTHROPIC_API_KEY not configured"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length).decode("utf-8") if length else "{}")
        except (json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"detail": f"Invalid JSON: {exc}"})
            return

        agent = req.get("agent") or {}
        items = req.get("items") or []
        settings = req.get("settings") or {}
        if not items:
            self._json(400, {"detail": "items is required"})
            return

        if user_id:
            from google_oauth import check_gmail_health

            health = check_gmail_health(user_id)
            if not health.get("healthy"):
                self._json(
                    401,
                    {"detail": health.get("error") or "Connect Gmail before running workflows", "health": health},
                )
                return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        client = anthropic.Anthropic(api_key=api_key)
        total = len(items)
        system = _build_system_prompt(agent.get("system_prompt", ""), settings, agent.get("name", "Agent"))
        queued = 0

        try:
            self.wfile.write(_sse_event("start", {"total": total, "agent": agent.get("name", ""), "user_id": user_id or ""}))
            self.wfile.flush()

            for i, item in enumerate(items):
                self.wfile.write(_sse_event("progress", {"current": i + 1, "total": total}))
                self.wfile.flush()
                user_content = (
                    f"Item data:\n{json.dumps(item.get('data', {}))}\n\n"
                    f"History:\n{item.get('history') or 'No prior actions.'}"
                )
                try:
                    response = client.messages.create(
                        model=MODEL,
                        max_tokens=1200,
                        system=system,
                        messages=[{"role": "user", "content": user_content}],
                    )
                    text = response.content[0].text if response.content else ""
                    result = _parse_agent_json(text)
                    action = result.get("action", "review")
                    if action not in ("wait", "none"):
                        queued += 1
                        self.wfile.write(
                            _sse_event(
                                "result",
                                {
                                    "item_id": item.get("item_id", ""),
                                    "reasoning": result.get("reasoning", ""),
                                    "action": action,
                                    "subject": result.get("subject", ""),
                                    "body": result.get("body", ""),
                                },
                            )
                        )
                        self.wfile.flush()
                except Exception as exc:
                    self.wfile.write(_sse_event("error", {"index": i + 1, "message": str(exc)}))
                    self.wfile.flush()

            self.wfile.write(_sse_event("done", {"total": total, "queued": queued}))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
