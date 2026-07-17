"""AI routes: Blueprint chat and agent pipeline (SSE)."""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys
import traceback
from urllib.parse import urlparse

_API_DIR = os.path.dirname(os.path.abspath(__file__))
_API_LIB = os.path.normpath(os.path.join(_API_DIR, "..", "api_lib"))
if _API_LIB not in sys.path:
    sys.path.insert(0, _API_LIB)

import anthropic

from action_registry import registry_for_prompt
from agent_pipeline import stream_agent_run
from execution_gate import check_execution_gate, record_allowed_action
from http_auth import resolve_user_id
from usage import record_api_call

MODEL = (os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-6").strip()
MAX_CHAT_TOKENS = 4096


def _log(msg: str) -> None:
    print(f"[ai] {msg}", flush=True)


def _blueprint_system_prompt() -> str:
    registry_block = "\n".join(
        f"{p['code']}: {p['name']} ({p['integration']})"
        + (" [requires approval]" if p["requires_approval"] else "")
        for p in registry_for_prompt()
    )
    return f"""You are LogiQ Blueprint — an intelligent colleague who turns plain-English automation ideas into structured workflows.

AVAILABLE PRIMITIVES (use ONLY these codes — never invent new actions):
{registry_block}

PHASE 1 AGENTS: aria (sales/outreach — leads, follow-ups, Gmail) or nova (customer comms — enquiries, support replies).

When the user describes what they want to automate:
1. If their request CANNOT be built using only the primitives above, respond conversationally explaining what is not supported yet and suggest a nearby alternative using available primitives. Do NOT output JSON.
2. If it CAN be built, respond with a brief friendly summary (2-3 sentences), then output a JSON block on its own line wrapped in ```json fences:

```json
{{
  "supported": true,
  "title": "Short workflow name",
  "summary": "What this workflow accomplishes",
  "agent": "aria" or "nova",
  "steps": [
    {{
      "step": 1,
      "code": "GS-01",
      "description": "Plain English: what this step does in context",
      "params": {{ "url": "https://docs.google.com/spreadsheets/d/..." }}
    }}
  ]
}}
```

Rules:
- Every step.code MUST be one of the registered primitives listed above. Do not use any code not in that list.
- Set requires_approval implicitly from the registry (GM-03, GM-04, GC-05, GC-06 always need approval).
- For steps that send email (GM-03, GM-04), include params: {{ "to", "subject", "body" }} with realistic draft content.
- For calendar: GC-01 needs time_min/time_max; GC-02 needs optional time range; GC-03/GC-06 need title, start, end (ISO); GC-06 also needs attendees[]; GC-04/GC-05 need event_id.
- For GS-01, include params: {{ "url" }} for the connected Google Sheet.
- Prefer 2–6 steps. Be practical, not generic.
- If the user asks for Gmail search/list/label/draft/thread or sheet writes, explain those are not available yet on this branch and offer a nearby alternative using the available primitives only.
- Tone: warm, concise, colleague-like — not a form or checklist.
- Never mention internal codes to the user in prose; codes belong only in JSON."""


def _response_text(response) -> str:
    parts = []
    for block in response.content or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(text)
    return "".join(parts)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        if path.endswith("/chat") or path.endswith("/chat/test"):
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"detail": f"Unknown route: {path}"})

    def do_POST(self):
        try:
            path = urlparse(self.path).path.rstrip("/")
            if path.endswith("/chat"):
                self._blueprint_chat()
            elif path.endswith("/agent/run"):
                self._agent_run()
            else:
                self._json(404, {"detail": f"Unknown route: {path}"})
        except Exception as exc:
            _log(f"POST unhandled: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            self._json(500, {"detail": f"{type(exc).__name__}: {exc}"})

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _blueprint_chat(self):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Valid Bearer token required"})
            return

        gate = check_execution_gate(user_id, "blueprint_chat")
        if not gate.allowed:
            self._json(403, gate.as_error_payload())
            return

        body = self._read_json_body()
        message = (body.get("message") or "").strip()
        if not message and body.get("messages"):
            for item in reversed(body.get("messages") or []):
                if (item.get("role") or "user") == "user" and (item.get("content") or "").strip():
                    message = item.get("content").strip()
                    break

        if not message:
            self._json(400, {"detail": "message is required"})
            return

        api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            _log("chat rejected: ANTHROPIC_API_KEY missing or empty")
            self._json(503, {"detail": "ANTHROPIC_API_KEY not configured"})
            return

        try:
            max_tokens = int(body.get("max_tokens") or 2200)
        except (TypeError, ValueError):
            max_tokens = 2200
        max_tokens = max(1, min(max_tokens, MAX_CHAT_TOKENS))

        system = (body.get("system") or "").strip() or _blueprint_system_prompt()
        raw_messages = body.get("messages") or [{"role": "user", "content": message}]
        claude_messages: list[dict] = []
        for m in raw_messages:
            role = "assistant" if (m.get("role") or "user") == "assistant" else "user"
            content = (m.get("content") or "").strip()
            if not content:
                continue
            if claude_messages and claude_messages[-1]["role"] == role:
                claude_messages[-1]["content"] += "\n\n" + content
            else:
                claude_messages.append({"role": role, "content": content})
        if not claude_messages:
            self._json(400, {"detail": "message is required"})
            return
        if claude_messages[0]["role"] != "user":
            claude_messages.insert(0, {"role": "user", "content": "(continuing conversation)"})

        _log(f"blueprint chat user={user_id} model={MODEL} messages={len(claude_messages)} max_tokens={max_tokens}")

        try:
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=claude_messages,
            )
            content = _response_text(response)
            record_allowed_action(gate.client_id, "blueprint_chat")
            record_api_call(user_id)
            self._json(200, {"content": content})
        except anthropic.APIError as exc:
            _log(f"chat Anthropic APIError: {exc}")
            self._json(502, {"detail": str(exc)})
        except Exception as exc:
            _log(f"chat failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            self._json(500, {"detail": str(exc) or "Chat request failed"})

    def _agent_run(self):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Valid Bearer token required"})
            return

        body = self._read_json_body()
        items = body.get("items") or []
        if not items:
            self._json(400, {"detail": "items is required"})
            return

        api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            self._json(503, {"detail": "ANTHROPIC_API_KEY not configured"})
            return

        try:
            client = anthropic.Anthropic(api_key=api_key)
            record_api_call(user_id)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            for chunk in stream_agent_run(user_id, body, anthropic_client=client, model=MODEL):
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except Exception as exc:
            _log(f"agent run failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            raise

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
