"""
Strips application-pen arguments out of a tool's JSON schema before it
is shown to a model. The model can't propose a value for a blank it
never sees, which is a stronger guarantee than "we validated what it
proposed and threw it away" -- it removes the incentive and the
opportunity in one step. Nested application-pen fields (under a
model-pen object) are left as-is in v1, since nesting application pen
under model pen isn't supported by the catalog.
"""

from __future__ import annotations

from typing import Any

from .catalog import Catalog, Pen


def hide_application_pen(catalog: Catalog, tool_name: str, input_schema: dict) -> dict:
    if tool_name not in catalog:
        return input_schema
    spec = catalog[tool_name]
    hidden = {name for name, arg in spec.args.items() if arg.pen == Pen.APPLICATION}

    props = {k: v for k, v in (input_schema.get("properties") or {}).items() if k not in hidden}
    required = [r for r in (input_schema.get("required") or []) if r not in hidden]
    return {**input_schema, "properties": props, "required": required}


def tools_for_model(catalog: Catalog, tool_defs: list[dict]) -> list[dict]:
    """
    tool_defs: Anthropic/OpenAI-style tool definitions, each with a
    "name" and an "input_schema" (or "parameters"). Returns the same
    list with application-pen arguments stripped from each schema.
    """
    out = []
    for t in tool_defs:
        schema_key = "input_schema" if "input_schema" in t else "parameters"
        schema = t.get(schema_key)
        if schema is None:
            out.append(t)
            continue
        out.append({**t, schema_key: hide_application_pen(catalog, t["name"], schema)})
    return out
