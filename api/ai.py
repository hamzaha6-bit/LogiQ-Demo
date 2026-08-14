"""AI routes: Blueprint chat and agent pipeline (SSE)."""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys
import traceback
from typing import Optional
from urllib.parse import parse_qs, urlparse

_API_DIR = os.path.dirname(os.path.abspath(__file__))
_API_LIB = os.path.normpath(os.path.join(_API_DIR, "..", "api_lib"))
if _API_LIB not in sys.path:
    sys.path.insert(0, _API_LIB)

import anthropic

from action_registry import registry_for_prompt
from agent_pipeline import stream_agent_run
from blueprint_history import (
    CLAUDE_CONTEXT_MESSAGE_CAP,
    append_message,
    cap_messages_for_claude,
    get_or_create_active_conversation,
    list_conversation_messages,
    load_active_history,
    normalize_agent_id,
    start_new_conversation,
)
from blueprint_plan import (
    enrich_plan_steps,
    extract_sheet_binding,
    parse_blueprint_plan,
    prepare_blueprint_response,
    strip_plan_json,
)
from execution_gate import check_blueprint_chat_gate, record_allowed_action
from http_auth import resolve_user_id
from supabase_rest import client_id_from_user_id
from usage import record_api_call

MODEL = (os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-6").strip()
MAX_CHAT_TOKENS = 4096


def _log(msg: str) -> None:
    print(f"[ai] {msg}", flush=True)


def _blueprint_system_prompt(*, bound_sheet: Optional[dict] = None) -> str:
    registry_lines = []
    for p in registry_for_prompt():
        line = (
            f"{p['code']}: {p['name']} ({p['integration']})"
            + (" [requires approval]" if p["requires_approval"] else "")
        )
        params = p.get("params")
        if isinstance(params, dict) and params:
            param_bits = "; ".join(f"{k}: {v}" for k, v in params.items())
            line += f"\n  params: {param_bits}"
        registry_lines.append(line)
    registry_block = "\n".join(registry_lines)
    bound_block = ""
    if bound_sheet and bound_sheet.get("spreadsheet_id"):
        bound_block = f"""

BOUND SPREADSHEET (already chosen in this conversation — preserve EXACTLY on every Sheets step when revising):
- url: {bound_sheet.get("url")}
- spreadsheet_id: {bound_sheet.get("spreadsheet_id")}
When the user asks to edit/regenerate filter logic or any other step, copy these values into every GS-* params.url / params.spreadsheet_id. Never replace them with YOUR_SHEET_ID, placeholders, or "https://docs.google.com/spreadsheets/d/...".
"""
    return f"""You are LogiQ Blueprint — an intelligent colleague who turns plain-English automation ideas into structured workflows.

AVAILABLE PRIMITIVES (use ONLY these codes — never invent new actions):
{registry_block}

Blueprint always builds for aria. Set "agent" to "aria" on every plan.

When the user describes what they want to automate:
1. If their request CANNOT be built using only the primitives above, respond conversationally explaining what is not supported yet and suggest a nearby alternative using available primitives. Do NOT output JSON.
2. If it CAN be built, respond with a brief friendly summary (2-3 sentences in plain English — no step codes, no template variables, no param names), then output a JSON block on its own line wrapped in ```json fences. The JSON is for the execution engine only; the user never sees it.

```json
{{
  "supported": true,
  "title": "Short workflow name",
  "summary": "What this workflow accomplishes, including that it processes one item per run",
  "agent": "aria",
  "steps": [
    {{
      "step": 1,
      "code": "GS-01",
      "description": "Plain English: what this step does in context",
      "params": {{ "url": "<paste the user's real Google Sheets URL here>" }}
    }}
  ]
}}
```

Rules:
- Every step.code MUST be one of the registered primitives listed above. Do not use any code not in that list.
- Set requires_approval implicitly from the registry (GM-03, GM-04, GS-06, GC-05, and GC-06 always need approval).
- For steps that send email (GM-03, GM-04), include params: {{ "to", "subject", "body" }} with realistic draft content.
- For draft email (GM-05), single mode: {{ "to", "subject", "body" }}. For credit-chase / one draft per flagged customer: pass rows from the prior XF-06 step — {{ "rows": "{{{{step_N.output.rows}}}}", "to_column": "Contact email", "subject": "Payment reminder — {{Customer}}", "body": "Hi {{Customer}}, your balance of {{Current balance GBP}} is {{Days overdue}} days overdue." }} using {{Column}} placeholders filled per row. Creates held drafts only (not sent).
- For search (GM-07), include structured params like {{ "from", "subject", "after", "before", "has_attachment", "query", "max_results" }}.
- Inter-step templates MUST use this exact shape: {{{{step_N.output.field}}}} or nested paths like {{{{step_1.output.results.0.message_id}}}}. Never invent other template syntax.
- The workflow engine does NOT fan out over arrays for most steps — each step runs once. Exception: GM-05 with a rows list creates one draft per row. After GM-07/GM-01, still bind later read/label/send steps to the first result only, e.g. message_id: "{{{{step_1.output.results.0.message_id}}}}".
- Search results are ordered oldest-first for fairness (process the oldest match first).
- For franchise / enquiry inbox workflows (or similar labelling loops), GM-07 query MUST exclude already-processed mail, e.g. query: (franchise OR franchising OR "become a franchisee" OR "franchise opportunity") -label:"Franchise Enquiry"
- Franchise Enquiry Auto-Response (and similar): title like "Franchise Enquiry Auto-Response"; summary must state plainly that it processes one enquiry per run (not real-time / not all-at-once). Typical flow: GM-07 search → GM-02 read first result → GM-03/GM-04 acknowledgement (approval) → GM-06 label → GS-02 log row → GM-05 follow-up draft.
- For read message (GM-02) use {{ "message_id" }}; for get thread (GM-08) use {{ "thread_id" }}; for label (GM-06) use {{ "message_id", "add_labels", "remove_labels" }}.
- For Sheets steps, always include params.url with the user's REAL Google Sheets URL (full https://docs.google.com/spreadsheets/d/... link). NEVER invent YOUR_SHEET_ID, YOUR_SPREADSHEET_ID, example.com, or ellipsis URLs — if the user has not given a link yet, ask for it in prose and do not emit a deployable plan. When revising an existing plan, copy the previous plan's exact params.url / spreadsheet_id onto every GS-* step.
- GS-02 needs row/row_data; GS-03 needs row + row_data; GS-06 needs row; GS-07 needs cell (A1) + value.
- For calendar: GC-01 needs time_min/time_max; GC-02 needs optional time range; GC-03/GC-06 need title, start, end (ISO); GC-06 also needs attendees[]; GC-04/GC-05 need event_id.
- XF-01 only filters by a status equality + numeric ID range on columns that ALREADY exist. It cannot compare two columns, do date math, or invent Flagged.
- For credit-review / over-limit / days-overdue / Flagged logic use XF-06: pass an ORDERED derive list so later columns can reference earlier derived ones in the same step (Over limit → Days overdue → Overdue >30 days → Flagged last). Chain rows/columns from GS-01 via {{{{step_1.output.rows}}}} / {{{{step_1.output.columns}}}}. Optionally set keep_when or filter_column=Flagged + filter_value=yes. Never plan XF-01 with status_column=Flagged unless Flagged already exists on the sheet. When the goal is chase emails for each flagged customer, follow XF-06 with GM-05 batch mode (rows + to_column + subject/body templates) — one held draft per flagged row, not sent.
- Pound Fabrics / warehouse picklist (Shopify order export → picklist tabs). Title like "Pound Fabrics Picklist". Typical flow (do NOT use volume-balance GS-10 or XF-01-as-Flagged):
  1) GS-01 read the user's sheet (real url; optional sheet_name for the orders tab).
  2) XF-02 group-aware drop: group_column=Name (order number), match_column=Lineitem name, op=contains, value="Express Shipping" — drop EVERY line of any order that has an Express Shipping line, not just that line.
  3) XF-05 aggregate: sku_column=Lineitem sku, qty_column=Lineitem quantity, min_count=4, format_string="{{qty}}m x {{count}}", write_summary_to_qty=true. Merge ONLY when the SAME SKU AND SAME quantity value appear 4+ times. Never merge on SKU alone. Groups of 3 identical qty stay unmerged.
  4) GS-10 emit with split_mode="sku_prefix_bands" (NOT target_rows_per_tab / keep_groups_intact): exception_field=Lineitem sku; sku_column=Lineitem sku; product_name_column=Lineitem name; sheet1_before_product_name="Plain Polycotton Fabric"; sku_prefix_breaks=["COT","DF","F","G","L","S"]; optional template_sheet_name="Picklist Template". Sheet 1 boundary is PRODUCT NAME (before Plain Polycotton Fabric), not SKU prefix. Sheets 2–8 are SKU-prefix bands (before COT / DF / F / G / L / S, then rest). Missing SKU → Exceptions tab.
  5) Optional GS-11 format on the emitted tabs (bold/borders/banding). Print margins come from the template tab duplicate, not GS-11.
- Prefer 2–6 steps (picklist may use up to 5). Be practical, not generic.
- Tone: warm, concise, colleague-like — not a form or checklist.
- Never mention internal codes, template variables, or raw JSON fields to the user in prose; codes belong only in the JSON block.{bound_block}"""


def _prior_plan_from_messages(messages: list) -> Optional[dict]:
    """Most recent assistant message that contains a supported plan JSON."""
    for msg in reversed(messages or []):
        if (msg.get("role") or "") != "assistant":
            continue
        plan = parse_blueprint_plan(str(msg.get("content") or ""))
        if plan:
            return enrich_plan_steps(plan)
    return None


def _text_sources_from_messages(messages: list) -> list:
    return [str(m.get("content") or "") for m in (messages or []) if m.get("content")]


def _response_text(response) -> str:
    parts = []
    for block in response.content or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(text)
    return "".join(parts)


def _resolve_persist_client_id(user_id: str, gate_client_id: str) -> str:
    if gate_client_id and gate_client_id != "owner-bypass":
        return gate_client_id
    try:
        return client_id_from_user_id(user_id)
    except ValueError:
        return ""


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path.endswith("/blueprint/history"):
            self._blueprint_history(parse_qs(parsed.query))
        elif path.endswith("/chat") or path.endswith("/chat/test"):
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"detail": f"Unknown route: {path}"})

    def do_POST(self):
        try:
            path = urlparse(self.path).path.rstrip("/")
            if path.endswith("/blueprint/new"):
                self._blueprint_new()
            elif path.endswith("/chat"):
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

    def _blueprint_history(self, query: dict):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Valid Bearer token required"})
            return
        raw_agent = (query.get("agent_id") or ["aria"])[0]
        agent_id = normalize_agent_id(raw_agent) or "aria"
        payload = load_active_history(user_id, agent_id)
        if payload.get("error") == "invalid_agent":
            self._json(400, {"detail": "agent_id must be one of aria, nova, finn, zara, cleo"})
            return
        # Never leak raw execution JSON into the chat UI; attach plan separately when present.
        safe_messages = []
        latest_plan = None
        for msg in payload.get("messages") or []:
            raw = str(msg.get("content") or "")
            plan = parse_blueprint_plan(raw) if msg.get("role") == "assistant" else None
            if plan:
                user_content, enriched, _err = prepare_blueprint_response(raw)
                entry = dict(msg)
                entry["content"] = user_content
                if enriched:
                    entry["plan"] = enriched
                    latest_plan = enriched
                safe_messages.append(entry)
            else:
                entry = dict(msg)
                if msg.get("role") == "assistant":
                    entry["content"] = strip_plan_json(raw) or raw
                safe_messages.append(entry)
        payload["messages"] = safe_messages
        if latest_plan:
            payload["plan"] = latest_plan
        self._json(200, payload)

    def _blueprint_new(self):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Valid Bearer token required"})
            return
        body = self._read_json_body()
        status, payload = start_new_conversation(user_id, body.get("agent_id") or "aria")
        self._json(status, payload)

    def _blueprint_chat(self):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Valid Bearer token required"})
            return

        gate = check_blueprint_chat_gate(user_id)
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

        agent_id = normalize_agent_id(body.get("agent_id")) or "aria"
        persist_client_id = _resolve_persist_client_id(user_id, gate.client_id)

        conversation_id = (body.get("conversation_id") or "").strip()
        if conversation_id:
            pass
        elif persist_client_id:
            conversation, conv_err = get_or_create_active_conversation(
                user_id, agent_id, client_id=persist_client_id
            )
            if conv_err or not conversation:
                _log(f"conversation ensure failed: {conv_err}")
                self._json(502, {"detail": conv_err or "Failed to open conversation"})
                return
            conversation_id = str(conversation["id"])

        if conversation_id and persist_client_id:
            _, persist_err = append_message(
                conversation_id=conversation_id,
                user_id=user_id,
                agent_id=agent_id,
                role="user",
                content=message,
                client_id=persist_client_id,
            )
            if persist_err:
                _log(f"user message persist failed: {persist_err}")
                self._json(502, {"detail": "Failed to save message"})
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

        system = (body.get("system") or "").strip()
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

        claude_messages = cap_messages_for_claude(claude_messages, CLAUDE_CONTEXT_MESSAGE_CAP)

        # Prior plan JSON lives in DB (raw assistant content). Client history is
        # display-only, so load from conversation to preserve sheet bindings on edit.
        prior_plan = None
        text_sources = _text_sources_from_messages(claude_messages)
        if conversation_id:
            try:
                stored = list_conversation_messages(conversation_id)
                # Exclude the user message we just appended when finding prior plan
                prior_plan = _prior_plan_from_messages(stored)
                text_sources = _text_sources_from_messages(stored) + text_sources
            except Exception as exc:
                _log(f"prior plan load failed: {exc}")
        bound_sheet = extract_sheet_binding(prior_plan, text_sources=text_sources)
        if system:
            # Frontend may send its own prompt copy — still inject the bound sheet
            # so regenerate/edit cannot invent YOUR_SHEET_ID.
            if bound_sheet and bound_sheet.get("spreadsheet_id"):
                system = (
                    system.rstrip()
                    + "\n\nBOUND SPREADSHEET (already chosen in this conversation — "
                    "preserve EXACTLY on every Sheets step when revising):\n"
                    f"- url: {bound_sheet.get('url')}\n"
                    f"- spreadsheet_id: {bound_sheet.get('spreadsheet_id')}\n"
                    "Never replace with YOUR_SHEET_ID or any placeholder."
                )
        else:
            system = _blueprint_system_prompt(bound_sheet=bound_sheet)

        _log(
            f"blueprint chat user={user_id} agent={agent_id} model={MODEL} "
            f"messages={len(claude_messages)} max_tokens={max_tokens} preview={gate.free_preview}"
            + (f" bound_sheet={bound_sheet.get('spreadsheet_id')}" if bound_sheet else "")
        )

        try:
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=claude_messages,
            )
            raw_content = _response_text(response)
            # Persist the full model output (incl. execution JSON) for Claude continuity.
            # Return only user-facing text + a separate plan field for approve/create.
            user_content, plan, plan_err = prepare_blueprint_response(
                raw_content,
                prior_plan=prior_plan,
                text_sources=text_sources,
            )
            if plan_err and not plan:
                user_content = (
                    strip_plan_json(raw_content)
                    or "I couldn't build that workflow yet — "
                    + plan_err
                    + ". Try rephrasing or ask for something Gmail, Sheets, or Calendar can do."
                )

            if conversation_id and persist_client_id and raw_content:
                _, asst_err = append_message(
                    conversation_id=conversation_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    role="assistant",
                    content=raw_content,
                    client_id=persist_client_id,
                )
                if asst_err:
                    _log(f"assistant message persist failed: {asst_err}")

            if not gate.free_preview:
                record_allowed_action(gate.client_id, "blueprint_chat")
            record_api_call(user_id)
            payload = {
                "content": user_content,
                "conversation_id": conversation_id or None,
                "agent_id": agent_id,
                "free_preview": gate.free_preview,
            }
            if plan:
                payload["plan"] = plan
            self._json(200, payload)
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
