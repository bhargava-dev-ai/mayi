"""
A model-agnostic agent loop that talks to a mayi gateway with the AGENT key.

The agent sees only the arguments it is allowed to propose (model and human
pens). Application-pen and derived arguments are never offered to it, and
anything it sends for them is discarded by the gateway anyway.
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any


def _post(base: str, key: str, path: str, body: dict) -> dict:
    r = urllib.request.Request(base + path, json.dumps(body).encode(), method="POST",
                               headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as resp:  # nosec B310 (operator-configured URL)
        return json.loads(resp.read())


def tool_schemas(catalog_view: dict, descriptions: dict[str, str] | None = None) -> list[dict]:
    """Anthropic tool schemas from the gateway's /v1/catalog view (agent-proposable args only)."""
    out = []
    for tool, args in catalog_view["tools"].items():
        props = {a: {"description": f"{a} ({pen}-controlled)"} for a, pen in args.items()
                 if pen in ("model", "human")}
        out.append({"name": tool, "description": (descriptions or {}).get(tool, tool),
                    "input_schema": {"type": "object", "properties": props, "required": list(props)}})
    return out


def run_agent(client: Any, model: str, base: str, agent_key: str, session_id: str,
              tools: list[dict], task: str, max_turns: int = 6, log=print,
              wait_for_approval: float = 0, poll: float = 2.0, session_token: str | None = None) -> list[dict]:
    """Run a tool-use loop; every tool call goes through the gateway. Returns the gateway outcomes."""
    messages = [{"role": "user", "content": task}]
    outcomes: list[dict] = []
    for _ in range(max_turns):
        resp = client.messages.create(model=model, max_tokens=1024, tools=tools, messages=messages)
        uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
        if not uses:
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for u in uses:
            body = {"session_id": session_id, "tool": u.name, "args": u.input}
            if session_token:
                body["session_token"] = session_token
            r = _post(base, agent_key, "/v1/call", body)
            if r.get("outcome") == "escalated" and wait_for_approval:
                # Hold the call open for a human decision. simulate never consumes
                # an approval; only the final /v1/call executes (once).
                log(f"  waiting up to {wait_for_approval:.0f}s for a human to approve {u.name}...")
                deadline = time.time() + wait_for_approval
                while time.time() < deadline:
                    time.sleep(poll)
                    if _post(base, agent_key, "/v1/simulate", body).get("outcome") == "simulated":
                        r = _post(base, agent_key, "/v1/call", body)
                        break
            outcomes.append(r)
            log(f"  model proposed {u.name}({json.dumps(u.input)}) -> {r['outcome']} {r.get('reason', '')}")
            results.append({"type": "tool_result", "tool_use_id": u.id, "content": json.dumps(r)})
        messages.append({"role": "user", "content": results})
    return outcomes
