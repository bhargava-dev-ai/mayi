import os
import time

import pytest

from mayi import (ApprovalSet, ApprovalToken, Catalog, CatalogError, Escalated,
                   Executed, Rejected, SqliteAuditLog, ToolBoundary,
                   hide_application_pen, tools_for_model)
from mayi.integrations.mcp import handle_call_tool
from mayi.integrations.mcp import token_for as mcp_token_for

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "catalog.yaml")


def resolver(session, tool_name, arg_name):
    mapping = {"recipient": session.get("customer_email"), "employee_id": session.get("employee_id")}
    if arg_name not in mapping:
        raise KeyError(arg_name)
    return mapping[arg_name]


def make_boundary(extra_tools=None, log=None):
    tools = {
        "send_message": lambda recipient, text: text,
        "recommend": lambda employee_id, amount, rationale: (employee_id, amount, rationale),
        "update_profile": lambda employee_id, contact: (employee_id, contact),
    }
    tools.update(extra_tools or {})
    return ToolBoundary(Catalog.from_yaml(FIXTURE), tools, resolver, audit_log=log)


SESSION = {"customer_email": "real@example.com", "employee_id": "E1"}


# ---------------------------------------------------------------- catalog validation

def test_missing_pen_is_rejected():
    with pytest.raises(CatalogError, match="missing required field 'pen'"):
        Catalog.from_dict({"t": {"x": {}}})


def test_invalid_pen_name_is_rejected():
    with pytest.raises(CatalogError, match="invalid pen"):
        Catalog.from_dict({"t": {"x": {"pen": "wizard"}}})


def test_application_pen_without_source_is_rejected():
    with pytest.raises(CatalogError, match="requires 'source'"):
        Catalog.from_dict({"t": {"x": {"pen": "application"}}})


def test_human_pen_with_fields_is_rejected():
    with pytest.raises(CatalogError, match="only supported for the model pen"):
        Catalog.from_dict({"t": {"x": {"pen": "human", "fields": {"a": {"pen": "model"}}}}})


def test_model_pen_object_without_fields_is_rejected():
    with pytest.raises(CatalogError, match="must list its `fields`"):
        Catalog.from_dict({"t": {"x": {"pen": "model", "type": "object"}}})


def test_nested_field_under_model_must_also_be_model_pen():
    with pytest.raises(CatalogError, match="must themselves be model-pen"):
        Catalog.from_dict({"t": {"x": {"pen": "model", "fields": {"a": {"pen": "application", "source": "s"}}}}})


def test_unenforceable_limit_is_rejected_at_load():
    with pytest.raises(ValueError, match="limit must compare the argument"):
        Catalog.from_dict({"t": {"amount": {"pen": "human", "limit": "other_var <= 500"}}})


def test_limit_with_non_numeric_bound_is_rejected():
    with pytest.raises(ValueError, match="must compare against a number"):
        Catalog.from_dict({"t": {"amount": {"pen": "human", "limit": "amount <= 'lots'"}}})


# ---------------------------------------------------------------- reachability / strict mode

def test_strict_boundary_refuses_an_unlisted_tool_at_construction():
    catalog = Catalog.from_yaml(FIXTURE)
    with pytest.raises(CatalogError, match="reachable but not in the catalog"):
        ToolBoundary(catalog, {"delete_everything": lambda: None}, resolver)


def test_non_strict_boundary_allows_it():
    catalog = Catalog.from_yaml(FIXTURE)
    ToolBoundary(catalog, {"send_message": lambda recipient, text: None}, resolver, strict=False)  # no raise


# ---------------------------------------------------------------- bounded standing approvals

def test_amount_within_limit_executes_without_any_token():
    b = make_boundary()
    result = b.call("recommend", {"amount": 200, "rationale": "fine"}, SESSION)
    assert isinstance(result, Executed)
    assert result.args["amount"] == 200


def test_amount_at_the_exact_boundary_executes():
    b = make_boundary()
    result = b.call("recommend", {"amount": 500, "rationale": "fine"}, SESSION)  # limit is <= 500
    assert isinstance(result, Executed)


def test_amount_over_limit_still_requires_a_token():
    b = make_boundary()
    result = b.call("recommend", {"amount": 501, "rationale": "fine"}, SESSION)
    assert isinstance(result, Escalated)


def test_standing_approval_is_noted_in_the_audit_log():
    records = []
    b = make_boundary(log=records.append)
    b.call("recommend", {"amount": 200, "rationale": "fine"}, SESSION)
    assert "standing approval" in records[0]["notes"]["amount"]


# ---------------------------------------------------------------- single-use / expiring tokens

def test_a_used_token_cannot_be_reused():
    b = make_boundary()
    approvals = ApprovalSet([ApprovalToken(tool="recommend", arg="amount", value=999, approved_by="risk_owner")])

    first = b.call("recommend", {"amount": 999, "rationale": "a"}, SESSION, approvals=approvals)
    second = b.call("recommend", {"amount": 999, "rationale": "b"}, SESSION, approvals=approvals)

    assert isinstance(first, Executed)
    assert isinstance(second, Escalated)  # the same token cannot fire twice


def test_an_expired_token_is_treated_as_absent():
    b = make_boundary()
    approvals = ApprovalSet([ApprovalToken(
        tool="recommend", arg="amount", value=999, approved_by="risk_owner",
        expires_at=time.time() - 1,
    )])

    result = b.call("recommend", {"amount": 999, "rationale": "a"}, SESSION, approvals=approvals)
    assert isinstance(result, Escalated)


def test_a_not_yet_expired_token_still_works():
    b = make_boundary()
    approvals = ApprovalSet([ApprovalToken(
        tool="recommend", arg="amount", value=999, approved_by="risk_owner",
        expires_at=time.time() + 60,
    )])

    result = b.call("recommend", {"amount": 999, "rationale": "a"}, SESSION, approvals=approvals)
    assert isinstance(result, Executed)


# ---------------------------------------------------------------- structured (nested) arguments

def test_undeclared_structured_value_is_rejected():
    # `rationale` has no declared fields, so a dict proposed for it is refused outright
    b = make_boundary()
    result = b.call("recommend", {"amount": 100, "rationale": {"sneaky": "payload"}}, SESSION)
    assert isinstance(result, Rejected)
    assert "structured value" in result.reason


def test_declared_nested_fields_are_validated_and_pass():
    b = make_boundary()
    result = b.call("update_profile", {"contact": {"email": "a@b.com", "phone": "555"}}, SESSION)
    assert isinstance(result, Executed)
    assert result.args["contact"] == {"email": "a@b.com", "phone": "555"}


def test_unknown_nested_field_is_rejected():
    b = make_boundary()
    result = b.call("update_profile", {"contact": {"email": "a@b.com", "ssn": "danger"}}, SESSION)
    assert isinstance(result, Rejected)
    assert "undeclared field" in result.reason


def test_oversized_nested_field_is_rejected():
    b = make_boundary()
    result = b.call("update_profile", {"contact": {"email": "x" * 200, "phone": "555"}}, SESSION)
    assert isinstance(result, Rejected)


def test_missing_fields_object_entirely_is_rejected():
    b = make_boundary()
    result = b.call("update_profile", {"contact": "not-an-object"}, SESSION)
    assert isinstance(result, Rejected)
    assert "expected an object" in result.reason


# ---------------------------------------------------------------- hiding application-pen args from the model

def test_hide_application_pen_removes_it_from_schema():
    catalog = Catalog.from_yaml(FIXTURE)
    schema = {
        "type": "object",
        "properties": {"recipient": {"type": "string"}, "text": {"type": "string"}},
        "required": ["recipient", "text"],
    }
    out = hide_application_pen(catalog, "send_message", schema)
    assert "recipient" not in out["properties"]
    assert "recipient" not in out["required"]
    assert "text" in out["properties"]


def test_tools_for_model_strips_across_a_whole_tool_list():
    catalog = Catalog.from_yaml(FIXTURE)
    defs = [{"name": "send_message", "input_schema": {
        "type": "object",
        "properties": {"recipient": {"type": "string"}, "text": {"type": "string"}},
        "required": ["recipient", "text"],
    }}]
    out = tools_for_model(catalog, defs)
    assert "recipient" not in out[0]["input_schema"]["properties"]


# ---------------------------------------------------------------- durable audit log

def test_sqlite_audit_log_records_and_queries(tmp_path):
    log = SqliteAuditLog(str(tmp_path / "audit.db"))
    b = make_boundary(log=log)

    b.call("send_message", {"recipient": "x", "text": "hi"}, SESSION)
    b.call("recommend", {"amount": 999, "rationale": "a"}, SESSION)  # escalates

    all_records = log.all()
    assert len(all_records) == 2
    assert all_records[0]["outcome"] == "Executed"
    assert log.rejected_and_escalated()[0]["outcome"] == "Escalated"
    log.close()


# ---------------------------------------------------------------- MCP adapter

def test_mcp_executed_call_shape():
    b = make_boundary()
    result = handle_call_tool(b, "send_message", {"recipient": "x", "text": "hi"}, SESSION)
    assert result["isError"] is False
    assert result["content"][0]["text"] == "hi"


def test_mcp_escalated_call_shape_and_approval_retry():
    b = make_boundary()
    result = handle_call_tool(b, "recommend", {"amount": 999, "rationale": "a"}, SESSION)
    assert result["isError"] is True
    assert "mayi_escalation" in result

    esc = result["mayi_escalation"]
    approvals = ApprovalSet([ApprovalToken(tool=esc["tool"], arg=esc["arg"], value=esc["proposed_value"], approved_by="risk_owner")])
    retried = handle_call_tool(b, "recommend", {"amount": 999, "rationale": "a"}, SESSION, approvals=approvals)
    assert retried["isError"] is False
