"""
Write a starter catalog from tool definitions you already have.

Reads tool schemas in any of these shapes (a list, or a dict with a "tools" list):
  Anthropic   {"name", "description", "input_schema": {"properties": {...}}}
  MCP         {"name", "description", "inputSchema": {"properties": {...}}}
  OpenAI      {"type": "function", "function": {"name", "parameters": {"properties": {...}}}}

SAFE BY DEFAULT: every argument starts as the human pen, so nothing runs without a person's
approval until you decide otherwise. For each argument it adds a comment with a suggestion
(never an applied setting), based only on the argument's name and type. mayi does not know
what your tools mean; you decide.
"""

from __future__ import annotations

import json
import re
from typing import Any

_APP_HINT = re.compile(r"(^|_)(id|ids|email|account|customer|user|ticket|order|session|tenant|owner|recipient)($|_)", re.I)
_TEXT_HINT = re.compile(r"^(note|notes|message|body|text|comment|description|reason|summary|subject|title|content)$", re.I)
_NUM_HINT = re.compile(r"(amount|total|price|cost|quantity|qty|count|limit|balance)", re.I)
_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


class SchemaError(ValueError):
    pass


def _tools(raw: Any) -> list[tuple[str, str, dict]]:
    items = raw.get("tools") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise SchemaError("expected a non-empty list of tool definitions (or {\"tools\": [...]})")
    out = []
    for t in items:
        if not isinstance(t, dict):
            raise SchemaError("each tool definition must be an object")
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        schema = fn.get("input_schema") or fn.get("inputSchema") or fn.get("parameters") or {}
        if not isinstance(name, str) or not _SAFE_NAME.match(name):
            raise SchemaError(f"unusable tool name: {name!r}")
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if not isinstance(props, dict):
            raise SchemaError(f"tool '{name}': properties must be an object")
        for p in props:
            if not _SAFE_NAME.match(str(p)):
                raise SchemaError(f"tool '{name}': unusable argument name {p!r}")
        out.append((name, str(fn.get("description", "")).strip().splitlines()[0][:100] if fn.get("description") else "", props))
    return out


def _suggest(arg: str, spec: Any) -> str:
    t = spec.get("type") if isinstance(spec, dict) else None
    if _TEXT_HINT.match(arg) and t in (None, "string"):
        return "looks like free text: could be `pen: model`, type: str, with a max_len"
    if _APP_HINT.search(arg):
        return "looks like it identifies someone or something: could be `pen: application` with a `source:` from your session"
    if t in ("number", "integer") or _NUM_HINT.search(arg):
        return "looks like an amount: keep `pen: human`; optionally add `limit:` and `session_budget:`"
    return "no suggestion: decide who should control this"


def generate_catalog(raw: Any) -> tuple[str, dict]:
    """Returns (catalog_yaml_text, tools_json_skeleton)."""
    lines = ["# Generated starter catalog. Every argument is `human` (needs a person) until you decide.",
             "# Change one argument at a time, then run:  mayi check catalog.yaml", ""]
    urls = {}
    for name, desc, props in _tools(raw):
        if desc:
            lines.append(f"# {desc}")
        lines.append(f"{name}:")
        if not props:
            lines.append("  _requires_approval: true  # no arguments to gate, so the call itself needs a person until you decide")
        for arg, spec in props.items():
            lines += [f"  {arg}:", f"    pen: human   # TODO decide. {_suggest(arg, spec)}"]
        lines.append("")
        urls[name] = {"url": f"http://127.0.0.1:9001/{name}"}
    return "\n".join(lines), urls
