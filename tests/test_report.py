import os

from mayi import ApprovalSet, ApprovalToken, Catalog, SqliteAuditLog, ToolBoundary
from mayi.report import generate_report

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "catalog.yaml")


def resolver(session, tool_name, arg_name):
    mapping = {"recipient": session.get("customer_email"), "employee_id": session.get("employee_id")}
    return mapping[arg_name]


SESSION = {"customer_email": "real@example.com", "employee_id": "E1"}


def test_report_summarizes_a_mixed_run(tmp_path):
    log = SqliteAuditLog(str(tmp_path / "audit.db"))
    catalog = Catalog.from_yaml(FIXTURE)
    boundary = ToolBoundary(
        catalog, {"send_message": lambda recipient, text: text,
                  "recommend": lambda employee_id, amount, rationale: amount},
        resolver, audit_log=log,
    )

    boundary.call("send_message", {"recipient": "x", "text": "hi"}, SESSION, actor="agent-1")  # Executed
    boundary.call("send_message", {"recipient": "x", "text": "hi", "cc": "y"}, SESSION, actor="agent-1")  # Rejected
    boundary.call("recommend", {"amount": 9000, "rationale": "big"}, SESSION, actor="agent-2")  # Escalated
    approvals = ApprovalSet([ApprovalToken(tool="recommend", arg="amount", value=9000, approved_by="risk_owner")])
    boundary.call("recommend", {"amount": 9000, "rationale": "big"}, SESSION, approvals=approvals, actor="agent-2")  # Executed

    report = generate_report(log.all(), catalog)

    assert report.total_calls == 4
    assert report.executed == 2
    assert report.rejected == 1
    assert report.escalated == 1
    assert report.by_tool["send_message"] == 2
    assert report.by_tool["recommend"] == 2
    assert report.actors["agent-1"] == 2
    assert report.actors["agent-2"] == 2
    assert report.approvers_seen["risk_owner"] == 1
    assert report.catalog_version == "1"
    assert report.catalog_owner == "bhargava"
    log.close()


def test_report_as_text_is_readable(tmp_path):
    log = SqliteAuditLog(str(tmp_path / "audit.db"))
    catalog = Catalog.from_yaml(FIXTURE)
    boundary = ToolBoundary(catalog, {"send_message": lambda recipient, text: text}, resolver, audit_log=log)
    boundary.call("send_message", {"recipient": "x", "text": "hi"}, SESSION, actor="agent-1")

    text = generate_report(log.all(), catalog).as_text()

    assert "mayi governance report" in text
    assert "version:      1" in text
    assert "executed:     1" in text
    log.close()


def test_rejection_reason_is_actually_captured_not_just_the_outcome_type(tmp_path):
    log = SqliteAuditLog(str(tmp_path / "audit.db"))
    catalog = Catalog.from_yaml(FIXTURE)
    boundary = ToolBoundary(catalog, {"send_message": lambda recipient, text: text}, resolver, audit_log=log)

    boundary.call("send_message", {"recipient": "x", "text": "hi", "cc": "attacker@evil.com"}, SESSION)

    record = log.all()[0]
    assert record["outcome"] == "Rejected"
    assert record["reason"] is not None
    assert "unknown argument" in record["reason"]

    report = generate_report(log.all(), catalog)
    assert any("unknown argument" in reason for reason in report.rejection_reasons)
    log.close()
