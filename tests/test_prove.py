import pytest
import yaml

from mayi import Catalog
from mayi.cli import main
from mayi.prove import InvariantError, check

CAT = Catalog.from_dict({
    "refund": {"ticket_id": {"pen": "application", "source": "t"},
               "amount": {"pen": "human", "limit": "amount <= 500", "session_budget": 1000},
               "note": {"pen": "model", "type": "str", "max_len": 80}},
    "wire": {"amount": {"pen": "human", "limit": "amount >= 0"}, "memo": {"pen": "model", "type": "str"},
             "to": {"pen": "model", "type": "str", "max_len": 40}},
})


def by(findings):
    return {(f.invariant, f.subject): f.ok for f in findings}


def test_passing_and_failing_invariants():
    r = by(check(CAT, {
        "no_tool": ["delete_db", "wire"],
        "never_model_writable": ["refund.amount", "wire.to"],
        "human_above": {"refund.amount": 500, "wire.amount": 500},
        "session_total_at_most": {"refund.amount": 1000, "wire.amount": 1000},
        "no_copy_from_untrusted": ["wire.to"],
        "max_model_text": 100}))
    assert r[("no_tool", "delete_db")] and not r[("no_tool", "wire")]
    assert r[("never_model_writable", "refund.amount")] and not r[("never_model_writable", "wire.to")]
    assert r[("human_above", "refund.amount")] and not r[("human_above", "wire.amount")]
    assert r[("session_total_at_most", "refund.amount")] and not r[("session_total_at_most", "wire.amount")]
    assert not r[("no_copy_from_untrusted", "wire.to")]
    assert r[("max_model_text", "refund.note")] and not r[("max_model_text", "wire.memo")]


def test_budget_tighter_than_limit_counts_and_too_loose_fails():
    assert by(check(CAT, {"human_above": {"refund.amount": 500}}))[("human_above", "refund.amount")]
    assert not by(check(CAT, {"human_above": {"refund.amount": 400}}))[("human_above", "refund.amount")]


def test_unknown_references_are_errors_not_passes():
    for inv in ({"never_model_writable": ["nope.x"]}, {"never_model_writable": ["refund.zzz"]},
                {"typo_rule": []}, {"human_above": {"refund": 5}}):
        with pytest.raises(InvariantError):
            check(CAT, inv)


def test_cli_exit_codes(tmp_path, capsys):
    c = tmp_path / "c.yaml"; c.write_text(yaml.safe_dump({"t": {"a": {"pen": "human"}}}))
    ok = tmp_path / "ok.yaml"; ok.write_text("human_above: {t.a: 0}\n")
    bad = tmp_path / "bad.yaml"; bad.write_text("never_model_writable: [t.a]\nno_tool: [t]\n")
    assert main(["prove", str(c), str(ok)]) == 0
    assert main(["prove", str(c), str(bad)]) == 1
    assert main(["prove", str(c), str(tmp_path / "missing.yaml")]) == 2


from hypothesis import given, settings, strategies as st
from mayi import Executed, ToolBoundary


@settings(max_examples=150, deadline=None)
@given(op=st.sampled_from(["<", "<=", ">", ">=", "==", "!="]), bound=st.integers(0, 50),
       budget=st.one_of(st.none(), st.integers(1, 80)), n=st.integers(-10, 100),
       values=st.lists(st.integers(-200, 200), min_size=1, max_size=6))
def test_prover_agrees_with_the_real_boundary(op, bound, budget, n, values):
    """If the prover says human_above N holds, no value over N may ever run without approval."""
    spec = {"pen": "human", "limit": f"amount {op} {bound}", **({"session_budget": budget} if budget else {})}
    cat = Catalog.from_dict({"t": {"amount": spec}})
    holds = check(cat, {"human_above": {"t.amount": n}})[0].ok
    b = ToolBoundary(cat, {"t": lambda **kw: "ok"})
    for v in values:
        r = b.call("t", {"amount": v}, {}, budget_key="s")
        if holds and isinstance(r, Executed):
            assert v <= n


def test_zero_argument_tool_needs_tool_level_approval():
    from mayi import ApprovalSet, ApprovalToken, Escalated
    cat = Catalog.from_dict({"unlock_door": {"_requires_approval": True}, "ping": {}})
    b = ToolBoundary(cat, {"unlock_door": lambda **kw: "unlocked", "ping": lambda **kw: "pong"})
    assert isinstance(b.call("ping", {}, {}), Executed)                    # nothing to gate: documented
    r = b.call("unlock_door", {}, {})
    assert isinstance(r, Escalated) and r.arg == "_call"
    tok = ApprovalSet([ApprovalToken(tool="unlock_door", arg="_call", value=True, approved_by="ops")])
    assert isinstance(b.call("unlock_door", {}, {}, approvals=tok), Executed)
    assert isinstance(b.call("unlock_door", {}, {}, approvals=tok), Escalated)  # single use
    res = {(f.invariant, f.subject): f.ok for f in check(cat, {"call_needs_approval": ["unlock_door", "ping"]})}
    assert res[("call_needs_approval", "unlock_door")] and not res[("call_needs_approval", "ping")]
    from mayi.diff import diff_catalogs
    assert diff_catalogs(cat, Catalog.from_dict({"unlock_door": {}, "ping": {}})).loosened
    with pytest.raises(Exception):
        Catalog.from_dict({"t": {"_requires_approval": "yes"}})
