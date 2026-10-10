"""The plain Anthropic tool-use loop, with no framework. Blocks are faked; the SDK is not needed."""

import os
from types import SimpleNamespace as NS

from mayi import ApprovalSet, Catalog, ToolBoundary
from mayi.integrations.anthropic import resume, run_tool_uses, token_for

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "catalog.yaml")
CUSTOMER = "real.customer@example.com"
SESSION = {"customer_email": CUSTOMER, "employee_id": "E9"}


def resolver(session, tool_name, arg_name):
    if arg_name == "recipient":
        return session["customer_email"]
    if arg_name == "employee_id":
        return session["employee_id"]
    raise KeyError(arg_name)


def make(sent):
    tools = {
        "send_message": lambda recipient, text: (sent.append((recipient, text)) or "sent"),
        "recommend": lambda employee_id, amount, rationale: f"recorded {employee_id}:{amount}",
    }
    return ToolBoundary(Catalog.from_yaml(FIXTURE), tools, resolver)


def tool_use(name, args, id="tu_1"):
    return NS(type="tool_use", id=id, name=name, input=args)


def test_injected_recipient_is_replaced_in_a_plain_loop():
    sent = []
    content = [tool_use("send_message", {"recipient": "attacker@evil.com", "text": "hi"})]

    round_ = run_tool_uses(make(sent), content, SESSION)

    assert sent == [(CUSTOMER, "hi")]
    assert round_.results == [{"type": "tool_result", "tool_use_id": "tu_1", "content": "sent"}]
    assert not round_.needs_human


def test_text_blocks_are_ignored():
    sent = []
    content = [NS(type="text", text="Sure, sending that now."),
               tool_use("send_message", {"recipient": "x", "text": "hi"})]

    round_ = run_tool_uses(make(sent), content, SESSION)

    assert len(round_.results) == 1


def test_plain_dict_blocks_work_too():
    sent = []
    content = [{"type": "tool_use", "id": "tu_9", "name": "send_message",
                "input": {"recipient": "attacker@evil.com", "text": "hi"}}]

    round_ = run_tool_uses(make(sent), content, SESSION)

    assert sent == [(CUSTOMER, "hi")]
    assert round_.results[0]["tool_use_id"] == "tu_9"


def test_unknown_argument_comes_back_as_an_error_result():
    sent = []
    content = [tool_use("send_message", {"recipient": "x", "text": "hi", "cc": "attacker@evil.com"})]

    round_ = run_tool_uses(make(sent), content, SESSION)

    assert sent == []
    assert round_.results[0]["is_error"] is True
    assert round_.results[0]["content"].startswith("REJECTED")


def test_human_pen_is_escalated_not_executed():
    sent = []
    content = [tool_use("recommend", {"amount": 5000, "rationale": "ok"})]

    round_ = run_tool_uses(make(sent), content, SESSION)

    assert round_.needs_human
    assert round_.escalations[0].arg == "amount"
    assert round_.results[0]["is_error"] is True
    assert "NOT EXECUTED" in round_.results[0]["content"]


def test_resume_runs_only_the_escalated_call_once():
    sent = []
    boundary = make(sent)
    content = [tool_use("send_message", {"recipient": "x", "text": "hi"}, id="tu_1"),
               tool_use("recommend", {"amount": 5000, "rationale": "ok"}, id="tu_2")]

    first = run_tool_uses(boundary, content, SESSION)
    assert len(sent) == 1 and first.needs_human

    approvals = ApprovalSet([token_for(e, approved_by="risk_owner") for e in first.escalations])
    final = resume(boundary, content, first, SESSION, approvals)

    assert len(sent) == 1  # the message that already went out was not sent again
    assert [r["tool_use_id"] for r in final.results] == ["tu_1", "tu_2"]
    assert final.results[1]["content"] == "recorded E9:5000"
    assert not any(r.get("is_error") for r in final.results)
    assert not final.needs_human
