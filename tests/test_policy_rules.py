import time

import pytest

from mayi import ApprovalSet, ApprovalToken, Catalog, CatalogError, Escalated, Executed, Rejected, ToolBoundary
from mayi.diff import diff_catalogs
from mayi.gateway import Gateway
from mayi.prove import check


def make(cat, **ctx):
    ran = []
    names = list(cat)
    return ToolBoundary(Catalog.from_dict(cat), {n: (lambda **kw: ran.append(kw) or "ok") for n in names}), ran


def test_rate_limit_per_session_and_dry_runs_do_not_count():
    b, ran = make({"t": {"x": {"pen": "model", "type": "str", "max_len": 5}, "_rate_limit": "2/60"}})
    for _ in range(5):
        b.simulate("t", {"x": "a"}, {}, budget_key="s")
    assert [type(b.call("t", {"x": "a"}, {}, budget_key="s")) for _ in range(3)] == [Executed, Executed, Rejected]
    assert isinstance(b.call("t", {"x": "a"}, {}, budget_key="other"), Executed)   # another session is unaffected
    assert len(ran) == 3


def test_rate_limit_window_slides():
    b, _ = make({"t": {"x": {"pen": "model", "type": "str", "max_len": 5}, "_rate_limit": "1/0.05"}})
    assert isinstance(b.call("t", {"x": "a"}, {}, budget_key="s"), Executed)
    assert isinstance(b.call("t", {"x": "a"}, {}, budget_key="s"), Rejected)
    time.sleep(0.08)
    assert isinstance(b.call("t", {"x": "a"}, {}, budget_key="s"), Executed)


def test_argument_rules_use_trusted_values_and_beat_a_human_yes():
    cat = {"refund": {"order_total": {"pen": "application", "source": "total"},
                      "amount": {"pen": "human", "limit": "amount <= 50"},
                      "_require": ["amount <= order_total", "amount > 0"]}}
    b, ran = make(cat)
    s = {"total": 80}
    assert isinstance(b.call("refund", {"amount": 30}, s), Executed)
    assert isinstance(b.call("refund", {"amount": 0}, s), Rejected)
    big = ApprovalSet([ApprovalToken(tool="refund", arg="amount", value=500, approved_by="m")])
    r = b.call("refund", {"amount": 500}, s, approvals=big)
    assert isinstance(r, Rejected) and "amount <= order_total" in r.reason and len(ran) == 1


def test_rule_validation():
    for bad in (["amount <= nope"], ["amount"], ["amount <= 5 <= 9"], ["__import__('os') == 1"], "amount <= 5"):
        with pytest.raises(CatalogError):
            Catalog.from_dict({"t": {"amount": {"pen": "human"}, "_require": bad}})
    for bad in ("0/5", "x", "5/0", "5"):
        with pytest.raises(CatalogError):
            Catalog.from_dict({"t": {"a": {"pen": "human"}, "_rate_limit": bad}})


def test_allowlist_from_session_data_fails_closed():
    b, ran = make({"send": {"to": {"pen": "model", "type": "str", "max_len": 80, "allowlist_from": "thread.people"}}})
    ctx = {"thread": {"people": ["a@x.test"]}}
    assert isinstance(b.call("send", {"to": "a@x.test"}, ctx), Executed)
    assert isinstance(b.call("send", {"to": "evil@x.test"}, ctx), Rejected)
    assert isinstance(b.call("send", {"to": "a@x.test"}, {}), Rejected)             # no list in context: nothing allowed
    assert isinstance(b.call("send", {"to": "a@x.test"}, {"thread": {"people": "a@x.test"}}), Rejected)  # not a list


def test_forbid_urls_and_emails_in_free_text():
    b, ran = make({"send": {"body": {"pen": "model", "type": "str", "max_len": 300, "forbid": ["url", "email"]}}})
    assert isinstance(b.call("send", {"body": "Sorry about the delay."}, {}), Executed)
    for bad in ("see https://evil.test/x", "go to www.evil.test", "mail me: a.b@evil.test"):
        assert isinstance(b.call("send", {"body": bad}, {}), Rejected)
    with pytest.raises(CatalogError):
        Catalog.from_dict({"t": {"a": {"pen": "model", "type": "str", "forbid": ["sql"]}}})


def test_two_person_rule_in_the_boundary_and_through_the_gateway():
    cat = {"wire": {"amount": {"pen": "human", "dual_control_above": 1000}}}
    b, ran = make(cat)
    one = ApprovalSet([ApprovalToken(tool="wire", arg="amount", value=5000, approved_by="alice")])
    r = b.call("wire", {"amount": 5000}, {}, approvals=one)
    assert isinstance(r, Escalated) and "two different approvers" in r.reason
    assert one.peek("wire", "amount", 5000) is not None      # the single approval was not burned
    two = ApprovalSet([ApprovalToken(tool="wire", arg="amount", value=5000, approved_by="a + b", approvers=("a", "b"))])
    assert isinstance(b.call("wire", {"amount": 5000}, {}, approvals=two), Executed)
    assert isinstance(b.call("wire", {"amount": 900}, {}, approvals=ApprovalSet([ApprovalToken(tool="wire", arg="amount", value=900, approved_by="a")])), Executed)

    gw = Gateway(b, "AG", "AP")
    gw.create_session({"session_id": "s", "context": {}})
    appr = lambda who: gw.add_approval({"session_id": "s", "tool": "wire", "arg": "amount", "value": 7000, "approved_by": who})
    c = lambda: gw.call({"session_id": "s", "tool": "wire", "args": {"amount": 7000}}, False)["outcome"]
    assert c() == "escalated"
    assert appr("alice")["approved"] is False
    assert appr("alice")["approved"] is False        # the same person twice is still one person
    assert c() == "escalated"
    assert appr("bob")["approved"] is True
    assert c() == "executed"


def test_diff_and_prove_cover_the_new_rules():
    old = Catalog.from_dict({"t": {"a": {"pen": "human", "dual_control_above": 10}, "m": {"pen": "model", "type": "str", "forbid": ["url"], "allowlist_from": "x"},
                                   "_rate_limit": "5/60", "_require": ["a <= 99"]}})
    new = Catalog.from_dict({"t": {"a": {"pen": "human"}, "m": {"pen": "model", "type": "str"}}})
    text = " ".join(diff_catalogs(old, new).loosened)
    for needle in ("two-person", "forbidden content", "session allowlist", "rate limit", "argument rule"):
        assert needle in text
    res = {(f.invariant, f.subject): f.ok for f in check(old, {"rate_limited": ["t"], "two_person_above": {"t.a": 10}})}
    assert res[("rate_limited", "t")] and res[("two_person_above", "t.a")]
    res = {(f.invariant, f.subject): f.ok for f in check(new, {"rate_limited": ["t"], "two_person_above": {"t.a": 10}})}
    assert not any(res.values())
