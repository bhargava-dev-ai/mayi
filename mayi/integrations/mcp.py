"""
Adapter for an MCP server's tool-call handler.

MCP's call_tool is a synchronous request/response with no native
pause-and-resume, so an Escalated outcome here comes back as an error
CallToolResult telling the caller a human must approve, the same pattern
as the plain Anthropic adapter. Once approved, call handle_call_tool
again with an ApprovalSet containing the token.
"""

from __future__ import annotations

from typing import Any, Optional

from ..approvals import ApprovalSet, ApprovalToken
from ..boundary import ToolBoundary
from ..results import Escalated, Executed, Rejected


def token_for(escalation: Escalated, approved_by: str) -> ApprovalToken:
    return ApprovalToken(
        tool=escalation.tool, arg=escalation.arg,
        value=escalation.proposed_value, approved_by=approved_by,
    )


def handle_call_tool(
    boundary: ToolBoundary,
    name: str,
    arguments: dict,
    session: Any,
    approvals: Optional[ApprovalSet] = None,
    actor: Optional[str] = None,
) -> dict:
    """Returns an MCP-shaped CallToolResult dict: {"content": [...], "isError": bool}."""
    outcome = boundary.call(name, dict(arguments or {}), session, approvals=approvals, actor=actor)

    if isinstance(outcome, Executed):
        return {"content": [{"type": "text", "text": str(outcome.result)}], "isError": False}

    if isinstance(outcome, Rejected):
        return {"content": [{"type": "text", "text": f"REJECTED by tool boundary: {outcome.reason}"}], "isError": True}

    # Escalated
    return {
        "content": [{"type": "text", "text": (
            f"NOT EXECUTED: {outcome.tool}.{outcome.arg}={outcome.proposed_value!r} "
            "needs a person's approval before this call can run."
        )}],
        "isError": True,
        "mayi_escalation": {"tool": outcome.tool, "arg": outcome.arg, "proposed_value": outcome.proposed_value},
    }
