"""
Adapter for a plain Anthropic tool-use loop: no agent framework at all.

After `client.messages.create(...)` returns, hand `response.content` to
`run_tool_uses`. Every `tool_use` block goes through the boundary instead
of straight to your function, and you get back the `tool_result` blocks to
send in the next user message.

  Executed  -> a normal tool_result
  Rejected  -> a tool_result with is_error=True, so the model can recover
  Escalated -> NOT executed. A tool_result with is_error=True goes back to
               the model, and the escalation is returned to you so a person
               can decide. If they approve, call `resume(...)`.

This module never imports the anthropic SDK. It reads blocks that are
either SDK objects or plain dicts, so it also works with any client that
returns the same shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..approvals import ApprovalSet, ApprovalToken
from ..boundary import ToolBoundary
from ..results import Escalated, Executed, Rejected


def _get(block: Any, key: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


@dataclass
class ToolRound:
    results: list = field(default_factory=list)          # tool_result blocks for the next user message
    escalations: list = field(default_factory=list)      # Escalated outcomes waiting on a person
    escalated_ids: list = field(default_factory=list)    # tool_use ids matching `escalations`

    @property
    def needs_human(self) -> bool:
        return bool(self.escalations)


def token_for(escalation: Escalated, approved_by: str) -> ApprovalToken:
    """A person's approval of exactly this tool, argument and value. Nothing broader."""
    return ApprovalToken(
        tool=escalation.tool, arg=escalation.arg,
        value=escalation.proposed_value, approved_by=approved_by,
    )


def run_tool_uses(
    boundary: ToolBoundary,
    content: list,
    session: Any,
    approvals: Optional[ApprovalSet] = None,
    actor: Optional[str] = None,
    only: Optional[set] = None,
) -> ToolRound:
    round_ = ToolRound()
    for block in content:
        if _get(block, "type") != "tool_use":
            continue
        use_id = _get(block, "id")
        if only is not None and use_id not in only:
            continue

        outcome = boundary.call(
            _get(block, "name"), dict(_get(block, "input") or {}), session,
            approvals=approvals, actor=actor,
        )

        if isinstance(outcome, Executed):
            round_.results.append({
                "type": "tool_result", "tool_use_id": use_id, "content": str(outcome.result),
            })
        elif isinstance(outcome, Rejected):
            round_.results.append({
                "type": "tool_result", "tool_use_id": use_id, "is_error": True,
                "content": f"REJECTED by tool boundary: {outcome.reason}",
            })
        elif isinstance(outcome, Escalated):
            round_.escalations.append(outcome)
            round_.escalated_ids.append(use_id)
            round_.results.append({
                "type": "tool_result", "tool_use_id": use_id, "is_error": True,
                "content": (
                    f"NOT EXECUTED: {outcome.tool}.{outcome.arg}={outcome.proposed_value!r} "
                    "needs a person's approval."
                ),
            })
    return round_


def resume(
    boundary: ToolBoundary,
    content: list,
    previous: ToolRound,
    session: Any,
    approvals: ApprovalSet,
    actor: Optional[str] = None,
) -> ToolRound:
    """
    Re-run ONLY the tool calls that were escalated, now with approvals.
    Calls that already executed are not run a second time.
    """
    fresh = run_tool_uses(
        boundary, content, session, approvals=approvals, actor=actor,
        only=set(previous.escalated_ids),
    )
    by_id = {r["tool_use_id"]: r for r in previous.results}
    by_id.update({r["tool_use_id"]: r for r in fresh.results})
    order = [_get(b, "id") for b in content if _get(b, "type") == "tool_use"]
    return ToolRound(
        results=[by_id[i] for i in order if i in by_id],
        escalations=fresh.escalations,
        escalated_ids=fresh.escalated_ids,
    )
