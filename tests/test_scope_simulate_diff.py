from mayi import (ApprovalSet, ApprovalToken, Catalog, Escalated, Executed, Rejected,
                  ToolBoundary, diff_catalogs)

CAT = {"refund": {
    "ticket_id": {"pen": "application", "source": "ticket_id"},
    "amount": {"pen": "human"},
    "note": {"pen": "model", "type": "str", "max_len": 50},
}}


def boundary(calls=None):
    calls = calls if calls is not None else []
    return ToolBoundary(Catalog.from_dict(CAT),
                        {"refund": lambda **kw: calls.append(kw) or "ok"}), calls


def tok(**kw):
    return ApprovalToken(tool="refund", arg="amount", value=500, approved_by="mgr", **kw)


def test_scoped_approval_holds_only_for_its_resource():
    b, calls = boundary()
    ok = b.call("refund", {"amount": 500, "note": "x"}, {"ticket_id": "T-9"},
                approvals=ApprovalSet([tok(scope={"ticket_id": "T-9"})]))
    other = b.call("refund", {"amount": 500, "note": "x"}, {"ticket_id": "T-10"},
                   approvals=ApprovalSet([tok(scope={"ticket_id": "T-9"})]))
    assert isinstance(ok, Executed) and isinstance(other, Escalated)
    assert len(calls) == 1


def test_scope_on_non_application_arg_fails_closed():
    b, _ = boundary()
    r = b.call("refund", {"amount": 500, "note": "x"}, {"ticket_id": "T-9"},
               approvals=ApprovalSet([tok(scope={"note": "x"})]))  # model-pen value can't scope
    assert isinstance(r, Escalated)


def test_simulate_executes_nothing_and_keeps_approval():
    b, calls = boundary()
    approvals = ApprovalSet([tok()])
    sim = b.simulate("refund", {"amount": 500, "note": "x"}, {"ticket_id": "T-9"}, approvals)
    assert isinstance(sim, Executed) and sim.simulated and sim.result is None
    assert calls == []
    real = b.call("refund", {"amount": 500, "note": "x"}, {"ticket_id": "T-9"}, approvals=approvals)
    assert isinstance(real, Executed) and not real.simulated  # token was not consumed


def test_simulate_reports_escalation_and_rejection_without_audit():
    log = []
    b = ToolBoundary(Catalog.from_dict(CAT), {"refund": lambda **kw: None}, audit_log=log.append)
    assert isinstance(b.simulate("refund", {"amount": 5, "note": "x"}, {"ticket_id": "T"}), Escalated)
    assert isinstance(b.simulate("refund", {"amount": 500, "note": "x" * 99}, {"ticket_id": "T"}, ApprovalSet([tok()])), Rejected)
    assert log == []


def test_diff_flags_loosening():
    new = dict(CAT)
    new["refund"] = {**CAT["refund"], "amount": {"pen": "model", "type": "float"}}
    d = diff_catalogs(Catalog.from_dict(CAT), Catalog.from_dict(new))
    assert d and any("human -> model" in c for c in d.loosened)
    assert not diff_catalogs(Catalog.from_dict(CAT), Catalog.from_dict(CAT))
