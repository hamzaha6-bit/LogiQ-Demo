"""Parse Blueprint plans and build user-facing summaries (no internal JSON)."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from action_registry import ACTION_REGISTRY, IRREVERSIBLE_CODES, validate_plan_steps
from sheets_service import (
    is_placeholder_spreadsheet_ref,
    parse_spreadsheet_id,
    resolve_spreadsheet_id,
    SheetsError,
)

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_SUPPORTED_BLOB_RE = re.compile(r"\{[\s\S]*\"supported\"[\s\S]*\}")
_STEP_CODE_RE = re.compile(r"\b(?:GM|GS|GC)-\d{2}\b")
_TEMPLATE_VAR_RE = re.compile(r"\{\{[^}]+\}\}")
_SHEET_URL_IN_TEXT_RE = re.compile(
    r"https?://docs\.google\.com/spreadsheets/d/[a-zA-Z0-9-_]+[^\s)\]\"']*",
    re.IGNORECASE,
)
_SHEETS_STEP_CODES = frozenset(
    {
        "GS-01", "GS-02", "GS-03", "GS-04", "GS-05", "GS-06", "GS-07",
        "GS-08", "GS-09", "GS-10", "GS-11",
    }
)

# Codes that write externally or send — call out in the summary even if not irreversible.
_USER_APPROVAL_CODES = IRREVERSIBLE_CODES | {
    "GS-02", "GS-03", "GS-07", "GS-08", "GS-09", "GS-10", "GS-11",
}
_SHEET_WRITE_CODES = frozenset({
    "GS-02", "GS-03", "GS-06", "GS-07", "GS-08", "GS-09", "GS-10", "GS-11",
})
_EMAIL_SEND_CODES = frozenset({"GM-03", "GM-04"})


def parse_blueprint_plan(text: str) -> Optional[Dict[str, Any]]:
    """Extract a supported:true plan object from Claude text, or None."""
    raw = (text or "").strip()
    if not raw:
        return None
    blob = ""
    fenced = _FENCED_JSON_RE.search(raw)
    if fenced:
        blob = fenced.group(1).strip()
    if not blob:
        m = _SUPPORTED_BLOB_RE.search(raw)
        blob = m.group(0) if m else ""
    if not blob:
        return None
    try:
        plan = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(plan, dict) or plan.get("supported") is not True:
        return None
    if not isinstance(plan.get("steps"), list):
        return None
    return plan


def strip_plan_json(text: str) -> str:
    """Remove JSON plan fences / supported blobs so users never see execution JSON."""
    raw = text or ""
    without_fences = _FENCED_JSON_RE.sub("", raw)
    # Also strip bare supported:true objects (legacy unfenced replies).
    cleaned = _SUPPORTED_BLOB_RE.sub("", without_fences)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def enrich_plan_steps(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize steps with registry metadata; mutates a shallow copy."""
    out = dict(plan)
    steps_in = out.get("steps") or []
    enriched: List[Dict[str, Any]] = []
    for i, step in enumerate(steps_in):
        if not isinstance(step, dict):
            continue
        code = str(step.get("code") or "").strip().upper()
        meta = ACTION_REGISTRY.get(code) or {}
        requires = bool(
            step.get("requires_approval")
            or code in IRREVERSIBLE_CODES
            or meta.get("requires_approval")
        )
        enriched.append(
            {
                "step": int(step.get("step") or i + 1),
                "code": code,
                "name": meta.get("name") or code,
                "integration": meta.get("integration") or "Unknown",
                "description": str(step.get("description") or "").strip(),
                "requires_approval": requires,
                "params": step.get("params") if isinstance(step.get("params"), dict) else {},
            }
        )
    out["steps"] = enriched
    agent = str(out.get("agent") or "aria").strip().lower()
    out["agent"] = agent if agent in ("aria", "nova") else "aria"
    out["title"] = str(out.get("title") or "Workflow plan").strip()
    out["summary"] = str(out.get("summary") or "").strip()
    out["supported"] = True
    return out


def _sheet_ref_from_params(params: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Return (url, spreadsheet_id) from step params when either is a real binding."""
    if not isinstance(params, dict):
        return None, None
    url = str(
        params.get("url")
        or params.get("sheet_url")
        or params.get("spreadsheet_url")
        or ""
    ).strip()
    sid_raw = str(
        params.get("spreadsheet_id") or params.get("sheet_id") or ""
    ).strip()
    url_ok = bool(url and not is_placeholder_spreadsheet_ref(url) and parse_spreadsheet_id(url))
    sid_ok = bool(
        sid_raw and not is_placeholder_spreadsheet_ref(sid_raw) and parse_spreadsheet_id(sid_raw)
    )
    resolved_from_url = parse_spreadsheet_id(url) if url_ok else None
    return (url if url_ok else None), (sid_raw if sid_ok else resolved_from_url)


def extract_sheet_binding(
    plan: Optional[Dict[str, Any]] = None,
    *,
    text_sources: Optional[List[str]] = None,
) -> Optional[Dict[str, str]]:
    """
    Find the conversation-bound spreadsheet from a prior plan and/or free text
    (user messages that pasted a Sheets link).
    """
    if isinstance(plan, dict):
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            code = str(step.get("code") or "").strip().upper()
            if code not in _SHEETS_STEP_CODES:
                continue
            url, sid = _sheet_ref_from_params(step.get("params") or {})
            if sid or url:
                resolved = sid or (parse_spreadsheet_id(url or "") or "")
                if not resolved:
                    continue
                out_url = url or f"https://docs.google.com/spreadsheets/d/{resolved}/edit"
                return {"url": out_url, "spreadsheet_id": resolved}

    for text in text_sources or []:
        for match in _SHEET_URL_IN_TEXT_RE.finditer(text or ""):
            candidate = match.group(0).rstrip(".,;")
            if is_placeholder_spreadsheet_ref(candidate):
                continue
            sid = parse_spreadsheet_id(candidate)
            if sid:
                return {"url": candidate, "spreadsheet_id": sid}
    return None


def inherit_sheet_bindings(
    plan: Dict[str, Any],
    prior_plan: Optional[Dict[str, Any]] = None,
    *,
    text_sources: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    When Blueprint regenerates steps, copy a real sheet URL/id from the prior plan
    (or pasted chat links) into any Sheets step that is missing one or still has a
    placeholder like YOUR_SHEET_ID.
    """
    binding = extract_sheet_binding(prior_plan, text_sources=text_sources)
    if not binding:
        # Also allow a good binding already present on some step of the new plan
        # to fill sibling steps that lost theirs during regenerate.
        binding = extract_sheet_binding(plan, text_sources=text_sources)
    if not binding:
        return plan

    out = dict(plan)
    steps_out: List[Dict[str, Any]] = []
    for step in out.get("steps") or []:
        if not isinstance(step, dict):
            continue
        step_copy = dict(step)
        code = str(step_copy.get("code") or "").strip().upper()
        if code in _SHEETS_STEP_CODES:
            params = (
                dict(step_copy.get("params") or {})
                if isinstance(step_copy.get("params"), dict)
                else {}
            )
            url, sid = _sheet_ref_from_params(params)
            needs_fix = not url or not sid
            for key in ("url", "sheet_url", "spreadsheet_url", "spreadsheet_id", "sheet_id"):
                if key in params and is_placeholder_spreadsheet_ref(str(params.get(key) or "")):
                    needs_fix = True
                    params.pop(key, None)
            if needs_fix:
                params["url"] = binding["url"]
                params["spreadsheet_id"] = binding["spreadsheet_id"]
            step_copy["params"] = params
        steps_out.append(step_copy)
    out["steps"] = steps_out
    return out


def validate_sheet_placeholders(steps: List[Dict[str, Any]]) -> Optional[str]:
    """Reject only explicit placeholders (YOUR_SHEET_ID etc.) during chat prepare."""
    for i, step in enumerate(steps or [], start=1):
        if not isinstance(step, dict):
            continue
        code = str(step.get("code") or "").strip().upper()
        if code not in _SHEETS_STEP_CODES:
            continue
        params = step.get("params") if isinstance(step.get("params"), dict) else {}
        for key in ("url", "sheet_url", "spreadsheet_url", "spreadsheet_id", "sheet_id"):
            value = str(params.get(key) or "").strip()
            if not value:
                continue
            if is_placeholder_spreadsheet_ref(value):
                return (
                    f"Step {i} ({code}): {key} is still a placeholder "
                    f"({value!r}). Paste the real Google Sheets link in Blueprint "
                    "and re-approve — do not use YOUR_SHEET_ID."
                )
    return None


def validate_sheet_bindings(steps: List[Dict[str, Any]]) -> Optional[str]:
    """Reject Sheets steps that still use placeholders or lack a resolvable id (deploy/run)."""
    placeholder_err = validate_sheet_placeholders(steps)
    if placeholder_err:
        return placeholder_err
    for i, step in enumerate(steps or [], start=1):
        if not isinstance(step, dict):
            continue
        code = str(step.get("code") or "").strip().upper()
        if code not in _SHEETS_STEP_CODES:
            continue
        params = step.get("params") if isinstance(step.get("params"), dict) else {}
        url = str(params.get("url") or params.get("sheet_url") or "").strip()
        sid = str(params.get("spreadsheet_id") or "").strip()
        try:
            resolve_spreadsheet_id(url or None, spreadsheet_id=sid or None)
        except SheetsError:
            return (
                f"Step {i} ({code}): needs a real Google Sheets URL "
                "(https://docs.google.com/spreadsheets/d/<id>/edit). "
                "Placeholders like YOUR_SHEET_ID are rejected before deploy/run."
            )
    return None


def _step_callout(step: Dict[str, Any]) -> str:
    code = step.get("code") or ""
    if code in _EMAIL_SEND_CODES:
        return " (needs your approval)"
    if code in _SHEET_WRITE_CODES:
        params = step.get("params") or {}
        url = str(params.get("url") or params.get("sheet_url") or "").strip()
        if not url or "docs.google.com/spreadsheets" not in url.lower():
            return " (needs your spreadsheet link)"
        if code == "GS-06":
            return " (needs your approval)"
        return ""
    if code in _USER_APPROVAL_CODES:
        return " (needs your approval)"
    return ""


def sanitize_user_facing_text(text: Any) -> str:
    """Strip step codes and template vars; collapse whitespace. Safe for UI copy."""
    raw = "" if text is None else str(text)
    cleaned = _STEP_CODE_RE.sub("", raw)
    cleaned = _TEMPLATE_VAR_RE.sub("", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" \t\r\n-–—:")
    return cleaned.strip()


def _capitalize_sentence(text: str) -> str:
    if not text:
        return text
    return text[0].upper() + text[1:] if len(text) > 1 else text.upper()


def _registry_meta(code: Optional[str]) -> Dict[str, Any]:
    return ACTION_REGISTRY.get(str(code or "").strip().upper()) or {}


def plain_step_label(
    step: Optional[Dict[str, Any]] = None,
    *,
    description: Optional[str] = None,
    summary: Optional[str] = None,
    action_name: Optional[str] = None,
    name: Optional[str] = None,
    integration: Optional[str] = None,
    code: Optional[str] = None,
) -> str:
    """
    Plain-English label for a plan step or approval row.
    Never includes step codes, param keys, template vars, or 'CODE · Step N'.
    """
    src = step if isinstance(step, dict) else {}
    code_u = str(code or src.get("code") or src.get("primitive_code") or "").strip().upper()
    meta = _registry_meta(code_u)

    for candidate in (
        description,
        summary,
        src.get("description"),
        src.get("summary"),
    ):
        label = _capitalize_sentence(sanitize_user_facing_text(candidate))
        if label:
            return label

    for candidate in (
        action_name,
        name,
        src.get("action_name"),
        src.get("name"),
        meta.get("name"),
    ):
        label = _capitalize_sentence(sanitize_user_facing_text(candidate))
        if label:
            return label

    return _generic_run_sentence(
        integration=integration or src.get("integration"),
        action_name=None,
        code=code_u,
    )


def _generic_run_sentence(
    *,
    integration: Any = None,
    action_name: Any = None,
    code: Optional[str] = None,
) -> str:
    """Minimal safe sentence when no description is available — never dumps payload."""
    meta = _registry_meta(code)
    agent = sanitize_user_facing_text(integration or meta.get("integration") or "")
    action = sanitize_user_facing_text(action_name or meta.get("name") or "")
    if agent and action:
        return f"This step will run: {agent} · {action}"
    if action:
        return f"This step will run: {action}"
    if agent:
        return f"This step will run via {agent}"
    return "This step needs your approval before it runs."


def plain_approval_label(
    approval_or_step: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> str:
    """Alias for Mission Control / approval UI trigger labels."""
    return plain_step_label(approval_or_step, **kwargs)


_CALENDAR_FIELD_LABELS = {
    "title": "Title",
    "summary": "Title",
    "start": "Starts",
    "end": "Ends",
    "start_time": "Starts",
    "end_time": "Ends",
    "attendees": "Attendees",
    "to": "Attendees",
    "location": "Location",
    "description": "Details",
    "event_id": "Event",
}


def _format_calendar_approval_payload(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    seen: set = set()
    for key, label in _CALENDAR_FIELD_LABELS.items():
        if key not in payload or key in seen:
            continue
        seen.add(key)
        val = sanitize_user_facing_text(payload.get(key))
        if not val:
            continue
        lines.append(f"{label}: {val}")
    return "\n".join(lines)


def format_approval_payload(
    payload: Any,
    code: Optional[str] = None,
    *,
    description: Optional[str] = None,
    summary: Optional[str] = None,
    action_name: Optional[str] = None,
    integration: Optional[str] = None,
) -> str:
    """
    User-visible approval draft text. Dedicated formatters for email/calendar;
    generic-safe fallback never JSON.dumps and never leaks codes/param keys/templates.
    """
    if not isinstance(payload, dict) or not payload:
        return "No preview available"

    code_u = str(code or "").strip().upper()

    if code_u in _EMAIL_SEND_CODES:
        to = sanitize_user_facing_text(payload.get("to")) or "—"
        subject = sanitize_user_facing_text(payload.get("subject")) or "—"
        body = sanitize_user_facing_text(payload.get("body"))
        return f"To: {to}\nSubject: {subject}\n\n{body}"

    if code_u.startswith("GC-"):
        calendar_text = _format_calendar_approval_payload(payload)
        if calendar_text:
            return calendar_text

    # Generic-safe fallback — description first, then agent · action sentence.
    for candidate in (description, summary):
        label = _capitalize_sentence(sanitize_user_facing_text(candidate))
        if label:
            return label
    return _generic_run_sentence(
        integration=integration,
        action_name=action_name,
        code=code_u,
    )


def _plain_step_line(step: Dict[str, Any]) -> str:
    return f"{plain_step_label(step)}{_step_callout(step)}"


def build_user_facing_summary(plan: Dict[str, Any]) -> str:
    """
    Plain-language card text for chat. No step codes, param names, or template vars.
    """
    enriched = enrich_plan_steps(plan) if plan.get("steps") else plan
    agent = enriched.get("agent") or "aria"
    agent_label = "Nova" if agent == "nova" else "Aria"
    title = enriched.get("title") or "Workflow plan"
    lines: List[str] = [f"{title} (via {agent_label})", ""]

    summary = str(enriched.get("summary") or "").strip()
    if summary and not _STEP_CODE_RE.search(summary) and not _TEMPLATE_VAR_RE.search(summary):
        # Lead-in from plan summary when it's already plain English.
        if not summary.lower().startswith("when ") and not summary.lower().startswith("i'll"):
            lines.append(summary)
            lines.append("")
        else:
            lines.append(summary.rstrip(":") + ("" if summary.rstrip().endswith(":") else ":"))
            lines.append("")

    if not any(l.startswith("When ") or l.lower().startswith("i'll") for l in lines):
        lines.append("Here's what I'll do:")
        lines.append("")

    steps = enriched.get("steps") or []
    for step in steps:
        num = step.get("step") or 0
        lines.append(f"{num}. {_plain_step_line(step)}")

    lines.append("")
    footer = (
        "I'll process one matching item at a time. "
        "If several come in, I'll work through them one per run."
    )
    # Avoid duplicating an equivalent note already in the summary.
    if "one" not in (summary or "").lower() or "per run" not in (summary or "").lower():
        lines.append(footer)

    text = "\n".join(lines).strip()
    # Hard guarantee: never ship codes or mustache templates in the user summary.
    text = _STEP_CODE_RE.sub("", text)
    text = _TEMPLATE_VAR_RE.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def display_content_for_user(raw_assistant_text: str, plan: Optional[Dict[str, Any]] = None) -> str:
    """
    What the end user should see in chat: prose (no JSON) plus generated summary when a plan exists.
    """
    prose = strip_plan_json(raw_assistant_text)
    if not plan:
        return prose
    summary = build_user_facing_summary(plan)
    # Prefer the structured card; keep a short prose lead-in if Claude wrote one
    # and it doesn't already duplicate the title/summary.
    if not prose:
        return summary
    # Drop prose that is mostly restating the same plan summary.
    prose_compact = re.sub(r"\s+", " ", prose).strip().lower()
    title = str(plan.get("title") or "").strip().lower()
    if title and title in prose_compact and len(prose) < 280:
        return summary
    return f"{prose}\n\n{summary}".strip()


def prepare_blueprint_response(
    raw_assistant_text: str,
    *,
    prior_plan: Optional[Dict[str, Any]] = None,
    text_sources: Optional[List[str]] = None,
) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    """
    Returns (user_facing_content, execution_plan_or_none, validation_error_or_none).
    execution_plan is enriched and safe to pass to workflow create on approve.
    When prior_plan / text_sources are provided, regenerated Sheets steps inherit
    the bound spreadsheet URL/id so edits cannot wipe in YOUR_SHEET_ID placeholders.
    """
    plan = parse_blueprint_plan(raw_assistant_text)
    if not plan:
        return strip_plan_json(raw_assistant_text) or (raw_assistant_text or "").strip(), None, None

    enriched = enrich_plan_steps(plan)
    enriched = inherit_sheet_bindings(
        enriched, prior_plan, text_sources=text_sources
    )
    enriched = enrich_plan_steps(enriched)
    err = validate_plan_steps(enriched.get("steps") or [])
    if err:
        return (
            strip_plan_json(raw_assistant_text)
            or "I couldn't build that workflow with the actions available today.",
            None,
            err,
        )
    # Chat may still show a plan that needs a sheet link; only hard-fail placeholders here.
    # Approve/create uses validate_sheet_bindings (full resolve required).
    sheet_err = validate_sheet_placeholders(enriched.get("steps") or [])
    if sheet_err:
        return (
            strip_plan_json(raw_assistant_text)
            or "I need your real Google Sheets link before I can finish this plan.",
            None,
            sheet_err,
        )
    user_content = display_content_for_user(raw_assistant_text, enriched)
    return user_content, enriched, None


def summary_leaks_internals(text: str) -> bool:
    """True if text still contains step codes or template variables."""
    return bool(_STEP_CODE_RE.search(text or "") or _TEMPLATE_VAR_RE.search(text or ""))
