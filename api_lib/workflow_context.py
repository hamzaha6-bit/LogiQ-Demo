"""Workflow execution context and mustache-style param template resolution."""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Tuple, Union

Context = Dict[str, Any]

_TEMPLATE_RE = re.compile(r"\{\{step_(\d+)\.output(?:\.([^}]+))?\}\}")
# Tolerate LLM variants like {{step1.results[0].message_id}} → mapped via normalize.
_ALT_TEMPLATE_RE = re.compile(
    r"\{\{step_?(\d+)\.(?:output\.)?(results|message_ids|messages)"
    r"(?:\[(\d+)\]|\.(\d+))?(?:\.(\w+))?\}\}"
)


def step_key(step_number: Union[int, str]) -> str:
    return f"step_{int(step_number)}"


def empty_context() -> Context:
    return {}


def set_step_output(context: Context, step_number: Union[int, str], output: Any) -> None:
    context[step_key(step_number)] = {"output": output}


def get_step_output(context: Context, step_number: Union[int, str]) -> Any:
    entry = context.get(step_key(step_number)) or {}
    return entry.get("output")


def _get_path(obj: Any, path_parts: List[str]) -> Any:
    cur = obj
    for part in path_parts:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError, TypeError):
                return None
        else:
            return None
    return cur


def _normalize_alt_templates(value: str) -> str:
    """Rewrite common LLM template variants into canonical {{step_N.output...}} form."""

    def repl(match: re.Match[str]) -> str:
        step_num = match.group(1)
        collection = match.group(2)
        idx = match.group(3) if match.group(3) is not None else match.group(4)
        field = match.group(5)
        parts = [f"step_{step_num}", "output", collection]
        if idx is not None:
            parts.append(idx)
        if field:
            # messages.0.id vs results.0.message_id
            if collection == "messages" and field == "message_id":
                parts.append("id")
            else:
                parts.append(field)
        elif collection == "results" and idx is not None and not field:
            parts.append("message_id")
        return "{{" + ".".join(parts) + "}}"

    return _ALT_TEMPLATE_RE.sub(repl, value)


def _lookup_template(match: re.Match[str], context: Context, warnings: List[str]) -> Tuple[Any, bool]:
    """Resolve one {{step_N.output...}} match to a raw value. Returns (value, empty_ref)."""
    step_num = match.group(1)
    field_path = match.group(2)
    output = get_step_output(context, step_num)
    if output is None:
        warnings.append(f"Unresolved template {match.group(0)}: step {step_num} has no output")
        return None, True
    if not field_path:
        return output, output == "" or output == []
    resolved = _get_path(output, field_path.split("."))
    if resolved is None:
        warnings.append(f"Unresolved template {match.group(0)}: field not found")
        return None, True
    if resolved == "" or resolved == []:
        return resolved, True
    return resolved, False


def _resolve_string(value: str, context: Context, warnings: List[str]) -> Tuple[Any, bool]:
    """Resolve templates in a string.

    When the entire string is a single template (e.g. \"{{step_1.output.rows}}\"),
    return the raw object so XF transforms can receive list/dict payloads from
    prior GS-01/XF steps. Interpolated templates still stringify.
    """
    text = _normalize_alt_templates(value)
    empty_ref = False
    whole = _TEMPLATE_RE.fullmatch(text.strip())
    if whole:
        resolved, was_empty = _lookup_template(whole, context, warnings)
        return resolved if not was_empty or resolved is not None else ("" if resolved is None else resolved), was_empty

    def repl(match: re.Match[str]) -> str:
        nonlocal empty_ref
        resolved, was_empty = _lookup_template(match, context, warnings)
        if was_empty:
            empty_ref = True
        if resolved is None:
            return ""
        if resolved == "" or resolved == []:
            return ""
        return str(resolved)

    return _TEMPLATE_RE.sub(repl, text), empty_ref


def resolve_params(
    params: Any,
    context: Context,
    warnings: Optional[List[str]] = None,
    *,
    empty_refs: Optional[List[bool]] = None,
) -> Any:
    """Resolve {{step_N.output.field}} references in step params."""
    warn = warnings if warnings is not None else []
    empties = empty_refs if empty_refs is not None else []

    if isinstance(params, str):
        resolved, was_empty = _resolve_string(params, context, warn)
        if was_empty:
            empties.append(True)
        return resolved
    if isinstance(params, list):
        return [resolve_params(item, context, warn, empty_refs=empties) for item in params]
    if isinstance(params, dict):
        return {
            key: resolve_params(val, context, warn, empty_refs=empties)
            for key, val in params.items()
        }
    return params


def resolved_params_copy(params: Any, context: Context) -> Any:
    warnings: List[str] = []
    resolved = resolve_params(copy.deepcopy(params or {}), context, warnings)
    for msg in warnings:
        print(f"[workflow_context] {msg}")
    return resolved


def resolve_params_with_meta(params: Any, context: Context) -> Tuple[Any, bool]:
    """Resolve params; second value is True if any template resolved empty/missing."""
    warnings: List[str] = []
    empties: List[bool] = []
    resolved = resolve_params(copy.deepcopy(params or {}), context, warnings, empty_refs=empties)
    for msg in warnings:
        print(f"[workflow_context] {msg}")
    return resolved, bool(empties)


def is_missing_upstream_id(code: str, params: Dict[str, Any], *, had_empty_ref: bool) -> bool:
    """
    True when a step needs a message/thread id that is empty after template resolution
    from a prior empty search (or unresolved template). Used for clean no-op exit.
    """
    normalized = (code or "").strip().upper()
    if not had_empty_ref and not _id_blank(params):
        return False
    if normalized in ("GM-02", "GM-04", "GM-06"):
        return not str(params.get("message_id") or params.get("id") or "").strip()
    if normalized == "GM-08":
        return not str(params.get("thread_id") or params.get("threadId") or "").strip()
    if normalized in ("GM-03", "GM-05"):
        # Draft/send often templated from prior read — only empty-exit when templates
        # failed AND to is blank.
        return had_empty_ref and not str(params.get("to") or "").strip()
    return False


def _id_blank(params: Dict[str, Any]) -> bool:
    mid = str(params.get("message_id") or params.get("id") or "").strip()
    tid = str(params.get("thread_id") or params.get("threadId") or "").strip()
    return (not mid) and (not tid)
