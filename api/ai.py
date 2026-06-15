"""AI routes: chat and agent pipeline (SSE)."""
from http.server import BaseHTTPRequestHandler
import json
import os
import re
import sys
import traceback
from urllib.parse import urlparse

MODEL = (os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-5-20250929").strip()
MAX_CHAT_TOKENS = 4096


def _log(msg: str) -> None:
    print(f"[ai] {msg}", flush=True)


def _ensure_api_lib() -> None:
    api_lib = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api_lib"))
    if api_lib not in sys.path:
        sys.path.insert(0, api_lib)


def _sse_event(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _response_text(response) -> str:
    parts = []
    for block in response.content or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(text)
    return "".join(parts)


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
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        if path.endswith("/chat/test"):
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"detail": f"Unknown route: {path}"})

    def do_POST(self):
        try:
            path = urlparse(self.path).path.rstrip("/")
            if path.endswith("/agent/run"):
                self._agent_run()
            elif path.endswith("/chat"):
                self._chat()
            else:
                self._json(404, {"detail": f"Unknown route: {path}"})
        except Exception as exc:
            _log(f"POST unhandled: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            self._json(500, {"detail": f"{type(exc).__name__}: {exc}"})

    def _chat(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            self._json(400, {"detail": f"Invalid JSON body: {exc}"})
            return

        api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            _log("chat rejected: ANTHROPIC_API_KEY missing or empty")
            self._json(503, {"detail": "ANTHROPIC_API_KEY not configured"})
            return

        system = body.get("system") or ""
        messages = body.get("messages") or []
        try:
            max_tokens = int(body.get("max_tokens") or 1200)
        except (TypeError, ValueError):
            max_tokens = 1200
        max_tokens = max(1, min(max_tokens, MAX_CHAT_TOKENS))

        if not messages:
            self._json(400, {"detail": "messages is required"})
            return

        _log(f"chat request model={MODEL} messages={len(messages)} max_tokens={max_tokens} system_len={len(system)}")

        import anthropic

        try:
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": m.get("role", "user"), "content": m.get("content", "")} for m in messages],
            )
            content = _response_text(response)
            self._json(200, {"content": content})
        except anthropic.APIError as exc:
            _log(f"chat Anthropic APIError: {exc}")
            self._json(502, {"detail": str(exc)})
        except Exception as exc:
            _log(f"chat failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            self._json(500, {"detail": str(exc) or "Chat request failed"})

    def _agent_run(self):
        _ensure_api_lib()
        from http_auth import resolve_user_id

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

        import anthropic

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
