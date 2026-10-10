"""
A drop-in replacement for langgraph.prebuilt.ToolNode.

The default ToolNode takes the model's tool_calls and executes them
directly. MayiToolNode routes every tool_call through the boundary
first:

  - Executed  -> a normal ToolMessage, exactly like ToolNode would produce
  - Rejected  -> a ToolMessage carrying the rejection reason, so the model
                 sees its call failed and can recover (or the graph can
                 route on it)
  - Escalated -> langgraph's native interrupt(), pausing the graph for a
                 human to approve or deny before anything executes. This
                 is the human-pen checkpoint from Gate IV, implemented
                 with LangGraph's own human-in-the-loop primitive rather
                 than a bespoke queue.

Nothing here is mayi-specific to LangGraph's message format beyond
translating AIMessage.tool_calls in and ToolMessage out. The boundary
itself (mayi.boundary.ToolBoundary) has no LangGraph import at all.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import interrupt

from ..approvals import ApprovalSet, ApprovalToken
from ..boundary import ToolBoundary
from ..results import Escalated, Executed, Rejected


class MayiToolNode:
    def __init__(
        self,
        boundary: ToolBoundary,
        session_from_state: Callable[[dict], Any],
        actor_from_state: Optional[Callable[[dict], str]] = None,
    ):
        """
        boundary: a configured ToolBoundary (catalog + real tool impls).
        session_from_state: (graph_state) -> session object the boundary's
            session_resolver expects, for application-pen resolution.
        actor_from_state: optional (graph_state) -> actor id, for the
            audit log.
        """
        self.boundary = boundary
        self.session_from_state = session_from_state
        self.actor_from_state = actor_from_state

    def __call__(self, state: dict) -> dict:
        messages = state["messages"]
        last = messages[-1]
        if not (isinstance(last, AIMessage) and last.tool_calls):
            raise TypeError(
                "MayiToolNode expects the last message to be an AIMessage with tool_calls, "
                f"got {type(last).__name__}"
            )

        session = self.session_from_state(state)
        actor = self.actor_from_state(state) if self.actor_from_state else None

        outgoing: list[ToolMessage] = []

        for call in last.tool_calls:
            outcome = self.boundary.call(
                call["name"], call["args"], session, actor=actor,
            )

            if isinstance(outcome, Executed):
                outgoing.append(ToolMessage(
                    content=str(outcome.result),
                    tool_call_id=call["id"],
                ))

            elif isinstance(outcome, Rejected):
                outgoing.append(ToolMessage(
                    content=f"REJECTED by tool boundary: {outcome.reason}",
                    tool_call_id=call["id"],
                    status="error",
                ))

            elif isinstance(outcome, Escalated):
                # Pause the graph. Whoever resumes it must supply either
                # an approval (the run continues and re-attempts the call)
                # or a denial (the run continues with a rejection message).
                # The model never sees or influences this decision path.
                decision = interrupt({
                    "type": "mayi_human_pen_approval_needed",
                    "tool": outcome.tool,
                    "arg": outcome.arg,
                    "proposed_value": outcome.proposed_value,
                })

                if decision.get("approved"):
                    approvals = ApprovalSet([ApprovalToken(
                        tool=outcome.tool,
                        arg=outcome.arg,
                        value=outcome.proposed_value,
                        approved_by=decision.get("approved_by", "unknown"),
                    )])
                    retry = self.boundary.call(
                        call["name"], call["args"], session,
                        approvals=approvals, actor=actor,
                    )
                    if isinstance(retry, Executed):
                        outgoing.append(ToolMessage(
                            content=str(retry.result), tool_call_id=call["id"],
                        ))
                    else:
                        outgoing.append(ToolMessage(
                            content=f"REJECTED after approval attempt: {retry}",
                            tool_call_id=call["id"], status="error",
                        ))
                else:
                    outgoing.append(ToolMessage(
                        content=(
                            f"DENIED by human reviewer: {outcome.tool}.{outcome.arg}"
                            f"={outcome.proposed_value!r} was not approved."
                        ),
                        tool_call_id=call["id"], status="error",
                    ))

        return {"messages": outgoing}
