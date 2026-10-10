import pytest

from mayi import (ApprovalSet, ApprovalToken, Catalog, CatalogError, Escalated, Executed, ToolBoundary)

CAT = {"refund": {"ticket_id": {"pen": "application", "source": "ticket_id"},
                  "amount": {"pen": "human", "limit": "amount <= 500", "session_budget": 1000}}}


def make(calls=None, cat=CAT):
    calls = calls if calls is not None else []
    return ToolBoundary(Catalog.from_dict(cat), {"refund": lambda **kw: calls.append(kw) or "ok"}), calls


def refund(b, amount, sid="s1", approvals=None):
    return b.call("refund", {"amount": amount}, {"ticket_id": "T-1"}, approvals=approvals, budget_key=sid)


def test_splitting_a_big_refund_into_small_ones_is_stopped():
    b, calls = make()
    results = [refund(b, 500) for _ in range(10)]  # each one is within the per-call limit
    assert [type(r) for r in results[:2]] == [Executed, Executed]
    assert all(isinstance(r, Escalated) for r in results[2:])
    assert len(calls) == 2 and "session budget used up" in results[2].reason


def test_each_session_has_its_own_budget():
    b, _ = make()
    assert isinstance(refund(b, 500, "a"), Executed) and isinstance(refund(b, 500, "a"), Executed)
    assert isinstance(refund(b, 500, "a"), Escalated)
    assert isinstance(refund(b, 500, "b"), Executed)


def test_human_approved_amounts_count_toward_the_total():
    b, _ = make()
    big = ApprovalSet([ApprovalToken(tool="refund", arg="amount", value=900, approved_by="mgr")])
    assert isinstance(refund(b, 900, approvals=big), Executed)  # over the per-call limit, a person said yes
    assert isinstance(refund(b, 100), Executed)                 # 900 + 100 == 1000, exactly at the cap
    assert isinstance(refund(b, 1), Escalated)                  # nothing left for standing approval


def test_negative_amounts_cannot_lower_the_running_total():
    b, calls = make()
    refund(b, 500); refund(b, 500)
    r = refund(b, -400)
    assert isinstance(r, Escalated) and "negative" in r.reason
    assert isinstance(refund(b, 100), Escalated) and len(calls) == 2


def test_dry_run_and_failed_tools_do_not_use_up_budget():
    b, _ = make()
    for _ in range(5):
        b.simulate("refund", {"amount": 500}, {"ticket_id": "T-1"}, budget_key="s1")
    assert b.spent("s1", "refund", "amount") == 0
    boom = ToolBoundary(Catalog.from_dict(CAT), {"refund": lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))})
    with pytest.raises(RuntimeError):
        boom.call("refund", {"amount": 500}, {"ticket_id": "T-1"}, budget_key="s1")
    assert boom.spent("s1", "refund", "amount") == 0


def test_without_a_session_identity_it_fails_closed():
    b, calls = make()
    r = b.call("refund", {"amount": 10}, {"ticket_id": "T-1"})  # no budget_key, session has no session_id
    assert isinstance(r, Escalated) and "no session identity" in r.reason and calls == []
    ok = b.call("refund", {"amount": 10}, {"ticket_id": "T-1", "session_id": "x"})  # picked up from the session
    assert isinstance(ok, Executed)


def test_reset_budget_starts_the_session_over():
    b, _ = make()
    refund(b, 500); refund(b, 500)
    assert isinstance(refund(b, 500), Escalated)
    b.reset_budget("s1")
    assert isinstance(refund(b, 500), Executed)


@pytest.mark.parametrize("bad", [
    {"amount": {"pen": "human", "session_budget": 100}},                          # no limit to cap
    {"amount": {"pen": "model", "type": "int", "limit": "amount <= 5", "session_budget": 100}},
    {"amount": {"pen": "human", "limit": "amount <= 5", "session_budget": 0}},
    {"amount": {"pen": "human", "limit": "amount <= 5", "session_budget": "lots"}},
    {"amount": {"pen": "human", "limit": "amount <= 5", "session_budget": True}},
])
def test_catalog_rejects_meaningless_budgets(bad):
    with pytest.raises(CatalogError):
        Catalog.from_dict({"refund": bad})


def test_diff_flags_loosening_a_budget():
    from mayi import diff_catalogs
    old = Catalog.from_dict(CAT)
    raised = Catalog.from_dict({"refund": {**CAT["refund"], "amount": {**CAT["refund"]["amount"], "session_budget": 5000}}})
    removed = Catalog.from_dict({"refund": {**CAT["refund"], "amount": {"pen": "human", "limit": "amount <= 500"}}})
    lowered = Catalog.from_dict({"refund": {**CAT["refund"], "amount": {**CAT["refund"]["amount"], "session_budget": 100}}})
    assert any("raised" in x for x in diff_catalogs(old, raised).loosened)
    assert any("removed" in x for x in diff_catalogs(old, removed).loosened)
    assert not diff_catalogs(old, lowered).loosened


def test_fuzz_standing_approvals_never_exceed_the_budget():
    hypothesis = pytest.importorskip("hypothesis")
    from hypothesis import given, settings, strategies as st
    amounts = st.one_of(st.integers(-10_000, 10_000), st.floats(allow_nan=True, allow_infinity=True, width=32), st.just(0))

    @settings(max_examples=200, deadline=None)
    @given(st.lists(amounts, min_size=1, max_size=40))
    def run(seq):
        b, calls = make()
        for a in seq:
            refund(b, a)
        assert sum(c["amount"] for c in calls) <= 1000
        assert all(c["amount"] >= 0 for c in calls)
    run()
