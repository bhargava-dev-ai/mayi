import threading

import pytest

from mayi import Catalog, ToolBoundary
from mayi.client import MayiClient
from mayi.gateway import Gateway, serve
from mayi.store import StateStore

CAT = {"send": {"to": {"pen": "application", "source": "email"}, "body": {"pen": "model", "type": "str", "max_len": 20}}}


def make(**kw):
    sent = []
    gw = Gateway(ToolBoundary(Catalog.from_dict(CAT), {"send": lambda **k: sent.append(k) or "ok"}), "AG", "AP", **kw)
    return gw, sent


def call(gw, sid, tok=None, role=None):
    b = {"session_id": sid, "tool": "send", "args": {"body": "hi"}}
    if tok:
        b["session_token"] = tok
    if role:
        b["_role"] = role
    return gw.call(b, False)["outcome"]


def test_bound_session_needs_its_own_token():
    gw, sent = make()
    a = gw.create_session({"session_id": "ticket-a", "context": {"email": "a@x.test"}, "bind": True})["session_token"]
    b = gw.create_session({"session_id": "ticket-b", "context": {"email": "b@x.test"}, "bind": True})["session_token"]
    assert call(gw, "ticket-a") == "rejected"             # no token
    assert call(gw, "ticket-a", b) == "rejected"          # another ticket's token cannot reach this ticket
    assert call(gw, "ticket-a", a) == "executed" and sent == [{"to": "a@x.test", "body": "hi"}]
    with pytest.raises(ValueError):
        gw.add_untrusted({"session_id": "ticket-a", "text": "x", "session_token": b})


def test_unbound_sessions_still_work_unless_tokens_are_required():
    gw, _ = make()
    gw.create_session({"session_id": "s", "context": {"email": "a@x.test"}})
    assert call(gw, "s") == "executed"
    gw2, _ = make(require_session_token=True)
    out = gw2.create_session({"session_id": "s", "context": {"email": "a@x.test"}})
    assert "session_token" in out and call(gw2, "s") == "rejected" and call(gw2, "s", out["session_token"]) == "executed"


def test_caller_cannot_claim_the_app_role_in_the_body_over_http():
    gw, sent = make()
    srv = serve(gw, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    tok = gw.create_session({"session_id": "s", "context": {"email": "a@x.test"}, "bind": True})["session_token"]
    agent = MayiClient(base, "AG")
    assert agent.call("s", "send", {"body": "hi"})["outcome"] == "rejected"
    import json, urllib.request
    r = urllib.request.Request(base + "/v1/call", data=json.dumps({"session_id": "s", "tool": "send", "args": {"body": "hi"}, "_role": "app"}).encode(),
                               headers={"Authorization": "Bearer AG", "Content-Type": "application/json"}, method="POST")
    assert json.loads(urllib.request.urlopen(r).read())["outcome"] == "rejected"   # the server overwrites _role
    agent.bind("s", tok)
    assert agent.call("s", "send", {"body": "hi"})["outcome"] == "executed"
    srv.shutdown()


def test_token_hashes_survive_restart_and_the_token_itself_is_not_stored(tmp_path):
    db = str(tmp_path / "s.db")
    st = StateStore(db)
    gw = Gateway(ToolBoundary(Catalog.from_dict(CAT), {"send": lambda **k: "ok"}, state=st), "AG", "AP", store=st)
    tok = gw.create_session({"session_id": "s", "context": {"email": "a@x.test"}, "bind": True})["session_token"]
    st.close()
    assert tok.encode() not in open(db, "rb").read()
    st2 = StateStore(db)
    gw2 = Gateway(ToolBoundary(Catalog.from_dict(CAT), {"send": lambda **k: "ok"}, state=st2), "AG", "AP", store=st2)
    assert call(gw2, "s") == "rejected" and call(gw2, "s", tok) == "executed"
