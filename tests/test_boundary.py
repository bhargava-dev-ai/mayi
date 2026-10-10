import os

import pytest

from mayi import ApprovalSet, ApprovalToken, Catalog, Escalated, Executed, Rejected, ToolBoundary

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "catalog.yaml")


class FakeSession:
    def __init__(self, customer_email, employee_id="E123"):
        self.ticket = type("Ticket", (), {"customer_email": customer_email})()
        self.employee_id = employee_id


def resolver(session, tool_name, arg_name):
    # dotted-path-ish resolution kept simple for the fixture
    if arg_name == "recipient":
        return session.ticket.customer_email
    if arg_name == "employee_id":
        return session.employee_id
    raise KeyError(arg_name)


def make_boundary(tool_impls, log=None):
    catalog = Catalog.from_yaml(FIXTURE)
    return ToolBoundary(catalog, tool_impls, resolver, audit_log=log)


# ---- application pen ----

def test_application_pen_ignores_model_supplied_value():
    calls = []
    boundary = make_boundary({"send_message": lambda recipient, text: calls.append((recipient, text))})
    session = FakeSession(customer_email="real.customer@example.com")

    result = boundary.call(
        "send_message",
        {"recipient": "attacker@evil.com", "text": "hi"},
        session,
    )

    assert isinstance(result, Executed)
    assert result.args["recipient"] == "real.customer@example.com"
    assert calls == [("real.customer@example.com", "hi")]


# ---- model pen ----

def test_model_pen_rejects_oversized_text():
    boundary = make_boundary({"send_message": lambda recipient, text: None})
    session = FakeSession(customer_email="c@example.com")

    result = boundary.call(
        "send_message",
        {"recipient": "irrelevant", "text": "x" * 3000},
        session,
    )

    assert isinstance(result, Rejected)
    assert "text" in result.reason


def test_model_pen_accepts_valid_text():
    boundary = make_boundary({"send_message": lambda recipient, text: text})
    session = FakeSession(customer_email="c@example.com")

    result = boundary.call(
        "send_message",
        {"recipient": "irrelevant", "text": "a normal reply"},
        session,
    )

    assert isinstance(result, Executed)
    assert result.result == "a normal reply"


# ---- human pen ----

def test_human_pen_escalates_without_approval():
    boundary = make_boundary({"recommend": lambda employee_id, amount, rationale: None})
    session = FakeSession(customer_email="c@example.com")

    result = boundary.call(
        "recommend",
        {"amount": 5000, "rationale": "seems fine"},
        session,
    )

    assert isinstance(result, Escalated)
    assert result.arg == "amount"
    assert result.proposed_value == 5000


def test_human_pen_executes_with_matching_approval():
    boundary = make_boundary({"recommend": lambda employee_id, amount, rationale: amount})
    session = FakeSession(customer_email="c@example.com")
    approvals = ApprovalSet([ApprovalToken(tool="recommend", arg="amount", value=200, approved_by="risk_owner")])

    result = boundary.call(
        "recommend",
        {"amount": 200, "rationale": "within policy"},
        session,
        approvals=approvals,
    )

    assert isinstance(result, Executed)
    assert result.result == 200


def test_human_pen_approval_does_not_cover_a_different_value():
    boundary = make_boundary({"recommend": lambda employee_id, amount, rationale: amount})
    session = FakeSession(customer_email="c@example.com")
    approvals = ApprovalSet([ApprovalToken(tool="recommend", arg="amount", value=200, approved_by="risk_owner")])

    # model proposes a different amount than the one approved
    result = boundary.call(
        "recommend",
        {"amount": 999, "rationale": "within policy"},
        session,
        approvals=approvals,
    )

    assert isinstance(result, Escalated)


# ---- unknown arguments ----

def test_unknown_argument_is_rejected():
    boundary = make_boundary({"send_message": lambda recipient, text: None})
    session = FakeSession(customer_email="c@example.com")

    result = boundary.call(
        "send_message",
        {"recipient": "irrelevant", "text": "hi", "cc": "attacker@evil.com"},
        session,
    )

    assert isinstance(result, Rejected)
    assert result.reason == "unknown argument(s)"


def test_unknown_tool_is_rejected():
    boundary = make_boundary({})
    session = FakeSession(customer_email="c@example.com")

    result = boundary.call("delete_everything", {}, session)

    assert isinstance(result, Rejected)
    assert result.reason == "unknown tool"


# ---- audit logging ----

def test_every_outcome_is_logged():
    records = []
    boundary = make_boundary({"send_message": lambda recipient, text: None}, log=records.append)
    session = FakeSession(customer_email="c@example.com")

    boundary.call("send_message", {"recipient": "x", "text": "hi"}, session, actor="user_42")

    assert len(records) == 1
    assert records[0]["actor"] == "user_42"
    assert records[0]["outcome"] == "Executed"
    assert records[0]["catalog_hash"] is not None
