"""
Adapters that put an agent framework's tools behind a mayi gateway.

Each takes the tool definitions in the Anthropic shape (name, description,
input_schema -- what `mayi.agent_client.tool_schemas` returns) and returns
framework tools whose every invocation goes through MayiClient.call. The
tool body never runs in your process: the gateway decides, and runs the real
tool only when the rules allow. A held or rejected call comes back to the
model as a plain message saying so.

    from mayi.adapters import openai_agents_tools, crewai_tools
    tools = openai_agents_tools(MayiClient(), "ticket-1042", schemas)

The framework packages are imported only when you call the adapter.
"""

from __future__ import annotations

import json
from typing import Any

from .client import MayiClient


def outcome_text(res: dict) -> str:
    """What the model is told. Keeps the reason, drops nothing a model needs to explain itself."""
    o = res.get("outcome")
    if o == "executed":
        return json.dumps({"status": "done", "result": res.get("result")}, default=str)
    if o == "escalated":
        return json.dumps({"status": "waiting for a person to approve", "argument": res.get("arg"),
                           "reason": res.get("reason")}, default=str)
    return json.dumps({"status": "not allowed", "reason": res.get("reason")}, default=str)


def _invoke(client: MayiClient, session_id: str, tool: str, args: dict) -> str:
    return outcome_text(client.call(session_id, tool, args))


def openai_agents_tools(client: MayiClient, session_id: str, schemas: list[dict]) -> list[Any]:
    from agents import FunctionTool  # type: ignore[import-not-found]

    tools = []
    for sch in schemas:
        name = sch["name"]

        async def on_invoke(ctx: Any, raw: str, _name: str = name) -> str:
            try:
                args = json.loads(raw or "{}")
            except json.JSONDecodeError:
                return json.dumps({"status": "not allowed", "reason": "arguments were not valid JSON"})
            if not isinstance(args, dict):
                return json.dumps({"status": "not allowed", "reason": "arguments must be an object"})
            return _invoke(client, session_id, _name, args)

        schema = dict(sch["input_schema"])
        schema.setdefault("additionalProperties", False)
        tools.append(FunctionTool(name=name, description=sch.get("description", name),
                                  params_json_schema=schema, on_invoke_tool=on_invoke,
                                  strict_json_schema=False))
    return tools


def crewai_tools(client: MayiClient, session_id: str, schemas: list[dict]) -> list[Any]:
    from crewai.tools import BaseTool  # type: ignore[import-not-found]
    from pydantic import Field, create_model

    tools = []
    for sch in schemas:
        name = sch["name"]
        props = sch["input_schema"].get("properties", {})
        Args = create_model(f"{name}_args", **{p: (Any, Field(default=None, description=d.get("description", p)))
                                               for p, d in props.items()})

        def _run(self: Any, _name: str = name, **kwargs: Any) -> str:
            return _invoke(client, session_id, _name, {k: v for k, v in kwargs.items() if v is not None})

        cls = type(f"Mayi_{name}", (BaseTool,), {
            "__module__": __name__,
            "__annotations__": {"name": str, "description": str, "args_schema": type},
            "name": name, "description": sch.get("description", name),
            "args_schema": Args, "_run": _run})
        tools.append(cls())
    return tools
