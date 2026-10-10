import os

import pytest

from mayi import ApprovalSet, ApprovalToken, Catalog, MayiEscalationRequired, MayiRejected, ToolBoundary, gate

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "catalog.yaml")


def resolver(session, tool_name, arg_name):
    if arg_name == "recipient":
        return session["customer_email"]
    if arg_name == "employee_id":
        return session["employee_id"]
    raise KeyError(arg_name)


def make_boundary():
    return ToolBoundary(
        Catalog.from_yaml(FIXTURE),
        {"send_message": lambda recipient, text: text,
         "recommend": lambda employee_id, amount, rationale: amount},
        resolver,
    )


SESSION = {"customer_email": "real@example.com", "employee_id": "E1"}


def test_gate_works_with_no_framework_at_all():
    """Nothing here is LangGraph, Anthropic, or MCP -- just a plain
    function someone decided to call, which is the whole point: gate()
    doesn't care what calls it."""
    boundary = make_boundary()
    action = gate(boundary, "send_message", session_provider=lambda: SESSION)

    result = action(recipient="attacker@evil.com", text="hi")

    assert result == "hi"


def test_gate_escalation_with_no_framework_at_all():
    boundary = make_boundary()
    action = gate(boundary, "recommend", session_provider=lambda: SESSION)

    with pytest.raises(MayiEscalationRequired) as exc:
        action(amount=9000, rationale="big")

    assert exc.value.tool == "recommend"
    assert exc.value.arg == "amount"


def test_gate_rejection_with_no_framework_at_all():
    boundary = make_boundary()
    action = gate(boundary, "send_message", session_provider=lambda: SESSION)

    with pytest.raises(MayiRejected):
        action(recipient="x", text="hi", cc="attacker@evil.com")


def test_gate_succeeds_on_retry_with_a_real_approval():
    boundary = make_boundary()
    action = gate(boundary, "recommend", session_provider=lambda: SESSION)

    with pytest.raises(MayiEscalationRequired):
        action(amount=9000, rationale="big")

    approvals = ApprovalSet([ApprovalToken(tool="recommend", arg="amount", value=9000, approved_by="risk_owner")])
    assert action(amount=9000, rationale="big", approvals=approvals) == 9000
