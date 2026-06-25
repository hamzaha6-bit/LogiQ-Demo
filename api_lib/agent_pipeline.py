"""Shared agent pipeline helpers (Anthropic SSE stream)."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Generator, Iterable, List, Optional

from execution_gate import check_execution_gate, record_allowed_action


def parse_agent_json(text: str) -> Dict[str, Any]:
    clean = re.sub(r"```json|```", "", text).strip()
    match = re.search(r"\{[\s\S]*\}", clean)
    if not match:
        raise ValueError("Could not parse JSON from response")
    return json.loads(match.group(0))


def build_system_prompt(base: str, settings: Dict[str, Any], agent_name: str = "") -> str:
    parts = [base or ""]
    tone = (settings.get("tone") or "").strip()
    cta = (settings.get("cta") or "").strip()
    business = (settings.get("business") or "").strip()
    if tone:
        parts.append(f"Tone: {tone}.")
    if cta:
        parts.append(f"CTA: {cta}.")
    if business:
        parts.append(f"Business: {business}.")
    calendly = (settings.get("calendly_link") or os.environ.get("CALENDLY_LINK") or "").strip()
    if agent_name == "Nova" and calendly:
        parts.append(
            f"If the message indicates interest in a meeting or call, append this Calendly booking link at the end of the response: {calendly}"
        )
    return "\n".join(p for p in parts if p)


def sse_event(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _response_text(response: Any) -> str:
    parts: List[str] = []
    for block in response.content or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(text)
    return "".join(parts)


def stream_agent_run(
    user_id: str,
    body: Dict[str, Any],
    *,
    anthropic_client: Any,
    model: str,
) -> Generator[str, None, None]:
    agent = body.get("agent") or {}
    settings = body.get("settings") or {}
    items = body.get("items") or []
    agent_name = (agent.get("name") or "").strip()
    system_prompt = build_system_prompt(
        agent.get("system_prompt") or "",
        settings,
        agent_name,
    )
    total = len(items)
    queued = 0

    yield sse_event("start", {"total": total, "agent": agent_name})

    for i, item in enumerate(items):
        yield sse_event("progress", {"current": i + 1, "total": total})

        gate = check_execution_gate(user_id, "agent_action")
        if not gate.allowed:
            yield sse_event("error", {"index": i + 1, **gate.as_error_payload()})
            continue

        user_content = (
            f"Item data:\n{json.dumps(item.get('data') or {})}\n\n"
            f"History:\n{item.get('history') or 'No prior actions.'}"
        )
        try:
            response = anthropic_client.messages.create(
                model=model,
                max_tokens=1200,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
            )
            text = _response_text(response)
            result = parse_agent_json(text)
            action = result.get("action", "review")

            if action not in ("wait", "none"):
                record_allowed_action(gate.client_id, "agent_action")
                queued += 1
                yield sse_event(
                    "result",
                    {
                        "item_id": item.get("item_id"),
                        "reasoning": result.get("reasoning", ""),
                        "action": action,
                        "subject": result.get("subject", ""),
                        "body": result.get("body", ""),
                    },
                )
        except Exception as exc:
            yield sse_event("error", {"index": i + 1, "message": str(exc)})

    yield sse_event("done", {"total": total, "queued": queued})
