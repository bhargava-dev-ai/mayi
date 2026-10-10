import json
import time

import pytest

from mayi import ApprovalToken, Catalog, ToolBoundary
from mayi.gateway import Gateway
from mayi.signing import sign_approval
from mayi.store import StateStore

CAT = {"refund": {"ticket_id": {"pen": "application", "source": "ticket_id"},
                  "amount": {"pen": "human", "limit": "amount <= 500", "session_budget": 1000}}}


def boot(path, ran=None):
    ran = ran if ran is not None else []
    st = StateStore(path)
    b = ToolBoundary(Catalog.from_dict(CAT), {"refund": lambda **kw: ran.append(kw) or "ok"}, state=st)
    return Gateway(b, "AG", "AP", signing_secret="s" * 32, store=st, pending_ttl=3600), st, ran


def call(gw, amount, sid="s1"):
    return gw.call({"session_id": sid, "tool": "refund", "args": {"amount": amount}}, False)["outcome"]


def test_budget_survives_a_restart(tmp_path):
    db = str(tmp_path / "s.db")
    gw, st, ran = boot(db)
    gw.create_session({"session_id": "s1", "context": {"ticket_id": "T"}})
    assert [call(gw, 500), call(gw, 500)] == ["executed", "executed"]
    st.close()
    gw2, _, ran2 = boot(db)                       # "crash" and restart
    assert call(gw2, 500) == "escalated" and ran2 == []   # the used-up budget was not reset


def test_pending_sessions_and_approvals_survive_restart_and_are_single_use(tmp_path):
    db = str(tmp_path / "s.db")
    gw, st, _ = boot(db)
    gw.create_session({"session_id": "s1", "context": {"ticket_id": "T"}})
    assert call(gw, 900) == "escalated" and len(gw.pending()) == 1
    gw.add_approval({"session_id": "s1", "tool": "refund", "arg": "amount", "value": 900, "approved_by": "mgr"})
    st.close()
    gw2, st2, ran2 = boot(db)
    assert call(gw2, 900) == "executed" and len(ran2) == 1     # approval carried over
    st2.close()
    gw3, _, ran3 = boot(db)
    assert call(gw3, 900) == "escalated" and ran3 == []        # and was used up for good


def test_signed_token_cannot_be_replayed_after_restart(tmp_path):
    db = str(tmp_path / "s.db")
    gw, st, _ = boot(db)
    gw.create_session({"session_id": "s1", "context": {"ticket_id": "T"}})
    tok = sign_approval("s" * 32, "refund", "amount", 900, "alice", session_id="s1")
    gw.redeem_approval({"token": tok})
    st.close()
    gw2, _, _ = boot(db)
    with pytest.raises(ValueError, match="already used"):
        gw2.redeem_approval({"token": tok})


def test_failed_tool_releases_its_reservation(tmp_path):
    st = StateStore(str(tmp_path / "s.db"))
    def boom(**kw): raise RuntimeError("down")
    b = ToolBoundary(Catalog.from_dict(CAT), {"refund": boom}, state=st)
    with pytest.raises(RuntimeError):
        b.call("refund", {"amount": 400}, {"ticket_id": "T"}, budget_key="s1")
    assert b.spent("s1", "refund", "amount") == 0


def test_held_calls_expire(tmp_path):
    st = StateStore(str(tmp_path / "s.db"))
    b = ToolBoundary(Catalog.from_dict(CAT), {"refund": lambda **kw: "ok"}, state=st)
    gw = Gateway(b, "AG", "AP", store=st, pending_ttl=0.05)
    gw.create_session({"session_id": "s1", "context": {"ticket_id": "T"}})
    call(gw, 900)
    assert len(gw.pending()) == 1
    time.sleep(0.1)
    assert gw.pending() == [] and st.load_pending() == {}
