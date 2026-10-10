"""
A framework-agnostic fix for the tool-bypass risk: whoever holds the
return value of gate() has no way to perform the action except through
mayi, because they were never given the real function, only this.

It applies to any agent, any framework, or no framework at all -- hand
the result of gate() to whatever calls your tools, instead of the tool
itself.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .approvals import ApprovalSet
from .boundary import ToolBoundary
from .results import Escalated, Executed, Rejected


class MayiRejected(Exception):
    def __init__(self, tool: str, reason: str):
        super().__init__(f"{tool}: {reason}")
        self.tool = tool
        self.reason = reason


class MayiEscalationRequired(Exception):
    """
    Raised instead of executing. Catch this, get a real approval, and
    call the gate again with `approvals=` set. There is no other path
    through: the raw tool function was never handed out, only the gate.
    """
    def __init__(self, tool: str, arg: str, proposed_value: Any):
        super().__init__(f"{tool}.{arg}={proposed_value!r} needs a person's approval")
        self.tool = tool
        self.arg = arg
        self.proposed_value = proposed_value


def gate(
    boundary: ToolBoundary,
    tool_name: str,
    session_provider: Callable[[], Any],
    actor_provider: Optional[Callable[[], str]] = None,
) -> Callable[..., Any]:
    """
    Returns a callable safe to hand to an agent, a framework, or anything
    else that will decide when to invoke this tool. It takes the tool's
    normal keyword arguments plus an optional `approvals=`.

    This is the concrete answer to "a tool reachable some other way is
    outside mayi's reach": don't give that other way a way in. Give it
    this instead, and there is no raw function anywhere for it to fall
    back on.
    """
    def gated(*, approvals: Optional[ApprovalSet] = None, **model_args) -> Any:
        session = session_provider()
        actor = actor_provider() if actor_provider else None
        outcome = boundary.call(tool_name, model_args, session, approvals=approvals, actor=actor)

        if isinstance(outcome, Executed):
            return outcome.result
        if isinstance(outcome, Rejected):
            raise MayiRejected(tool_name, outcome.reason)
        raise MayiEscalationRequired(outcome.tool, outcome.arg, outcome.proposed_value)

    gated.__name__ = f"mayi_gated__{tool_name}"
    return gated


def check_no_direct_exposure(boundary: ToolBoundary, exposed: list) -> list[str]:
    """
    A best-effort lint, not a guarantee: checks whether any of the exact
    function objects passed to this boundary have also been handed out
    somewhere else -- e.g. registered directly with an agent framework's
    tool list, alongside (instead of instead of) the gated version.

    `exposed`: whatever your framework was actually configured with --
    a list of tools, callbacks, or anything iterable containing them.

    This catches the single most common way the bypass risk actually
    happens in practice (the same callable handed to two places by
    accident). It cannot catch every path to a raw function in a
    dynamic language -- an import elsewhere, a closure, a reference
    kept and passed on later -- so a clean result here is evidence, not
    proof, that nothing bypasses the boundary.
    """
    warnings = []
    raw_by_identity = {id(fn): name for name, fn in boundary.tools.items()}
    for item in exposed:
        if id(item) in raw_by_identity:
            warnings.append(
                f"'{raw_by_identity[id(item)]}' is exposed directly (not through mayi.gate) "
                "in the list passed to check_no_direct_exposure"
            )
    return warnings
