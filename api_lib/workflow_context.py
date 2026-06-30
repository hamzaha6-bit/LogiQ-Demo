"""Workflow execution context and mustache-style param template resolution."""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Union

Context = Dict[str, Any]

_TEMPLATE_RE = re.compile(r"\{\{step_(\d+)\.output(?:\.([^}]+))?\}\}")


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


def _resolve_string(value: str, context: Context, warnings: List[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        step_num = match.group(1)
        field_path = match.group(2)
        output = get_step_output(context, step_num)
        if output is None:
            warnings.append(f"Unresolved template {match.group(0)}: step {step_num} has no output")
            return match.group(0)
        if not field_path:
            return str(output)
        resolved = _get_path(output, field_path.split("."))
        if resolved is None:
            warnings.append(f"Unresolved template {match.group(0)}: field not found")
            return match.group(0)
        return str(resolved)

    return _TEMPLATE_RE.sub(repl, value)


def resolve_params(params: Any, context: Context, warnings: Optional[List[str]] = None) -> Any:
    """Resolve {{step_N.output.field}} references in step params."""
    warn = warnings if warnings is not None else []

    if isinstance(params, str):
        return _resolve_string(params, context, warn)
    if isinstance(params, list):
        return [resolve_params(item, context, warn) for item in params]
    if isinstance(params, dict):
        return {key: resolve_params(val, context, warn) for key, val in params.items()}
    return params


def resolved_params_copy(params: Any, context: Context) -> Any:
    warnings: List[str] = []
    resolved = resolve_params(copy.deepcopy(params or {}), context, warnings)
    for msg in warnings:
        print(f"[workflow_context] {msg}")
    return resolved
