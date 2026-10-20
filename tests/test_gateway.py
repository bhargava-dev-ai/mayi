import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mayi import Catalog, ToolBoundary
from mayi.gateway import Gateway, http_tool, serve

CAT = {"refund": {
    "ticket_id": {"pen": "application", "source": "ticket_id"},
    "amount": {"pen": "human"},
    "note": {"pen": "model", "type": "str", "max_len": 50},
}}
AGENT, APP = "agent-key-1", "app-key-2"


@pytest.fixture
def stack():
    hits = []

    class Up(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            hits.append(body)
            out = json.dumps({"refunded": body["amount"]}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers()
            self.wfile.write(out)

    up = HTTPServer(("127.0.0.1", 0), Up)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    b = ToolBoundary(Catalog.from_dict(CAT),
                     {"refund": http_tool(f"http://127.0.0.1:{up.server_port}/refund")})
    gw = serve(Gateway(b, AGENT, APP), port=0)
    threading.Thread(target=gw.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{gw.server_port}"
    yield base, hits
    gw.shutdown(); up.shutdown()


def req(base, path, key, body=None):
    r = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                               method="GET" if body is None else "POST",
                               headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def setup(base):
    req(base, "/v1/sessions", APP, {"session_id": "s1", "context": {"ticket_id": "T-9"}})


def test_escalates_then_executes_once_with_approval(stack):
    base, hits = stack
    setup(base)
    c = {"session_id": "s1", "tool": "refund", "args": {"amount": 500, "note": "late"}}
    assert req(base, "/v1/call", AGENT, c)[1]["outcome"] == "escalated"
    assert hits == []
    req(base, "/v1/approvals", APP, {"session_id": "s1", "tool": "refund", "arg": "amount",
                                     "value": 500, "approved_by": "mgr", "scope": {"ticket_id": "T-9"}})
    assert req(base, "/v1/simulate", AGENT, c)[1]["outcome"] == "simulated"
    assert hits == []
    assert req(base, "/v1/call", AGENT, c)[1]["outcome"] == "executed"
    assert hits == [{"ticket_id": "T-9", "amount": 500, "note": "late"}]
    assert req(base, "/v1/call", AGENT, c)[1]["outcome"] == "escalated"  # single use


def test_agent_cannot_use_app_endpoints_or_read_audit(stack):
    base, _ = stack
    assert req(base, "/v1/sessions", AGENT, {"session_id": "x", "context": {}})[0] == 403
    assert req(base, "/v1/approvals", AGENT, {})[0] == 403
    assert req(base, "/v1/audit", AGENT)[0] == 403
    assert req(base, "/v1/call", APP, {})[0] == 403
    assert req(base, "/v1/call", "wrong", {})[0] == 401


def test_agent_value_for_application_arg_is_discarded_and_unknown_session_rejected(stack):
    base, hits = stack
    setup(base)
    r = req(base, "/v1/call", AGENT, {"session_id": "nope", "tool": "refund", "args": {}})[1]
    assert r["outcome"] == "rejected"
    req(base, "/v1/approvals", APP, {"session_id": "s1", "tool": "refund", "arg": "amount",
                                     "value": 5, "approved_by": "mgr"})
    req(base, "/v1/call", AGENT, {"session_id": "s1", "tool": "refund",
                                  "args": {"amount": 5, "ticket_id": "T-EVIL", "note": "x"}})
    assert hits == [{"ticket_id": "T-9", "amount": 5, "note": "x"}]  # agent's T-EVIL discarded; session value used


def test_audit_readable_by_app(stack):
    base, _ = stack
    setup(base)
    req(base, "/v1/call", AGENT, {"session_id": "s1", "tool": "refund", "args": {"amount": 1, "note": "x"}})
    code, ev = req(base, "/v1/audit", APP)
    assert code == 200 and ev and ev[-1]["outcome"].lower() == "escalated"


def test_keys_must_differ():
    with pytest.raises(ValueError):
        Gateway(ToolBoundary(Catalog.from_dict(CAT), {"refund": lambda **k: 1}), "same", "same")


def test_pending_queue_and_console(stack):
    base, _ = stack
    setup(base)
    c = {"session_id": "s1", "tool": "refund", "args": {"amount": 77, "note": "x"}}
    req(base, "/v1/call", AGENT, c); req(base, "/v1/call", AGENT, c)
    assert len(req(base, "/v1/pending", APP)[1]) == 1  # deduplicated
    assert req(base, "/v1/pending", AGENT)[0] == 403
    req(base, "/v1/approvals", APP, {"session_id": "s1", "tool": "refund", "arg": "amount", "value": 77, "approved_by": "m"})
    assert req(base, "/v1/pending", APP)[1] == []
    html = urllib.request.urlopen(base + "/console").read().decode()
    assert "mayi console" in html


def test_decide_gives_plain_answers_and_changes_nothing(stack):
    base, hits = stack
    setup(base)
    d = lambda args: req(base, "/v1/decide", AGENT, {"session_id": "s1", "tool": "refund", "args": args})[1]
    assert d({"amount": 40, "note": "x"})["decision"] == "hold"
    assert d({"amount": 40, "note": "x", "evil": 1})["decision"] == "reject"
    req(base, "/v1/approvals", APP, {"session_id": "s1", "tool": "refund", "arg": "amount", "value": 40, "approved_by": "m"})
    assert d({"amount": 40, "note": "x"})["decision"] == "allow"
    assert d({"amount": 40, "note": "x"})["decision"] == "allow"  # not consumed
    assert hits == []
    assert req(base, "/v1/decide", APP, {})[0] == 403


def test_client_uses_only_agent_key(stack, monkeypatch):
    from mayi.client import MayiClient
    base, hits = stack
    setup(base)
    c = MayiClient(base, AGENT)
    assert c.decide("s1", "refund", {"amount": 5, "note": "x"})["decision"] == "hold"
    req(base, "/v1/approvals", APP, {"session_id": "s1", "tool": "refund", "arg": "amount", "value": 5, "approved_by": "m"})
    assert c.call("s1", "refund", {"amount": 5, "note": "x"})["outcome"] == "executed"
    assert len(hits) == 1
    monkeypatch.delenv("MAYI_API_KEY", raising=False)
    with pytest.raises(ValueError):
        MayiClient(base)


def test_gateway_budget_is_per_session_and_survives_many_small_calls():
    from mayi import Catalog, ToolBoundary
    cat = {"refund": {"ticket_id": {"pen": "application", "source": "ticket_id"},
                      "amount": {"pen": "human", "limit": "amount <= 500", "session_budget": 1000}}}
    gw = Gateway(ToolBoundary(Catalog.from_dict(cat), {"refund": lambda **k: "ok"}), AGENT, APP)
    gw.create_session({"session_id": "a", "context": {"ticket_id": "T-1"}})
    gw.create_session({"session_id": "b", "context": {"ticket_id": "T-2"}})
    out = [gw.call({"session_id": "a", "tool": "refund", "args": {"amount": 500}}, False)["outcome"] for _ in range(4)]
    assert out == ["executed", "executed", "escalated", "escalated"]
    assert gw.call({"session_id": "b", "tool": "refund", "args": {"amount": 500}}, False)["outcome"] == "executed"


def test_gateway_budget_key_totals_across_sessions_for_one_customer():
    from mayi import Catalog, ToolBoundary
    cat = {"refund": {"ticket_id": {"pen": "application", "source": "ticket_id"},
                      "amount": {"pen": "human", "limit": "amount <= 500", "session_budget": 1000}}}
    gw = Gateway(ToolBoundary(Catalog.from_dict(cat), {"refund": lambda **k: "ok"}), AGENT, APP)
    gw.create_session({"session_id": "a", "budget_key": "cust-7", "context": {"ticket_id": "T-1"}})
    gw.create_session({"session_id": "b", "budget_key": "cust-7", "context": {"ticket_id": "T-2"}})
    gw.create_session({"session_id": "c", "budget_key": "cust-8", "context": {"ticket_id": "T-3"}})
    def go(sid):
        return gw.call({"session_id": sid, "tool": "refund", "args": {"amount": 500}}, False)["outcome"]
    assert [go("a"), go("b"), go("a")] == ["executed", "executed", "escalated"]  # one customer, two sessions, one total
    assert go("c") == "executed"                                                  # another customer is unaffected
    with pytest.raises(ValueError):
        gw.create_session({"session_id": "d", "budget_key": "", "context": {}})
