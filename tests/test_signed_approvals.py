import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from mayi import Catalog, ToolBoundary
from mayi.cli import main
from mayi.gateway import Gateway, serve
from mayi.signing import InvalidApproval, sign_approval, verify_approval

SECRET = "s3cret-for-tests"
AGENT, APP = "agent-key", "app-key"
CAT = {"refund": {"ticket_id": {"pen": "application", "source": "ticket_id"}, "amount": {"pen": "human"}}}


def test_roundtrip_and_tamper_evidence():
    t = sign_approval(SECRET, "refund", "amount", 40, "alice", session_id="s1")
    a, jti = verify_approval(SECRET, t, session_id="s1")
    assert (a.tool, a.arg, a.value, a.approved_by) == ("refund", "amount", 40, "alice") and jti
    body, sig = t.split(".")
    for bad in (body + "." + sig[:-2] + "AA", "x" + body[1:] + "." + sig, t + "x", "garbage", ""):
        with pytest.raises(InvalidApproval):
            verify_approval(SECRET, bad)
    with pytest.raises(InvalidApproval):
        verify_approval("other-secret", t)


def test_expiry_and_session_binding():
    t = sign_approval(SECRET, "refund", "amount", 40, "alice", ttl_seconds=10, session_id="s1", now=1000)
    with pytest.raises(InvalidApproval, match="expired"):
        verify_approval(SECRET, t, now=1011)
    with pytest.raises(InvalidApproval, match="different session"):
        verify_approval(SECRET, t, session_id="s2", now=1005)
    verify_approval(SECRET, t, session_id="s1", now=1005)


def test_cannot_forge_by_editing_the_value():
    import base64
    t = sign_approval(SECRET, "refund", "amount", 40, "alice")
    body, sig = t.split(".")
    p = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    p["value"] = 9000
    forged = base64.urlsafe_b64encode(json.dumps(p, sort_keys=True, separators=(",", ":")).encode()).rstrip(b"=").decode()
    with pytest.raises(InvalidApproval):
        verify_approval(SECRET, forged + "." + sig)


@pytest.fixture
def gw():
    hits = []
    b = ToolBoundary(Catalog.from_dict(CAT), {"refund": lambda **k: hits.append(k) or "ok"})
    g = Gateway(b, AGENT, APP, signing_secret=SECRET)
    g.create_session({"session_id": "s1", "context": {"ticket_id": "T-1"}})
    return g, hits


def ask(g, amount=40):
    return g.call({"session_id": "s1", "tool": "refund", "args": {"amount": amount}}, False)["outcome"]


def test_redeemed_token_lets_the_call_run_exactly_once(gw):
    g, hits = gw
    assert ask(g) == "escalated" and len(g.pending()) == 1
    tok = sign_approval(SECRET, "refund", "amount", 40, "alice", session_id="s1")
    assert g.redeem_approval({"token": tok})["approved_by"] == "alice"
    assert g.pending() == []
    assert ask(g) == "executed" and ask(g) == "escalated" and len(hits) == 1
    with pytest.raises(ValueError, match="already used"):
        g.redeem_approval({"token": tok})  # replay


def test_wrong_value_wrong_session_and_forged_tokens_are_refused(gw):
    g, hits = gw
    g.redeem_approval({"token": sign_approval(SECRET, "refund", "amount", 40, "alice", session_id="s1")})
    assert ask(g, 41) == "escalated"  # approval is for exactly 40
    g.create_session({"session_id": "s2", "context": {"ticket_id": "T-2"}})
    other = sign_approval(SECRET, "refund", "amount", 40, "alice", session_id="s2")
    with pytest.raises(ValueError, match="different session"):
        g.redeem_approval({"token": other, "session_id": "s1"})
    with pytest.raises(ValueError):
        g.redeem_approval({"token": sign_approval("wrong", "refund", "amount", 40, "x", session_id="s1")})
    assert hits == []


def test_disabled_without_a_secret():
    g = Gateway(ToolBoundary(Catalog.from_dict(CAT), {"refund": lambda **k: 1}), AGENT, APP)
    g.create_session({"session_id": "s1", "context": {"ticket_id": "T"}})
    with pytest.raises(ValueError, match="not enabled"):
        g.redeem_approval({"token": "a.b"})


def test_http_redeem_needs_the_app_key(gw):
    g, _ = gw
    s = serve(g, port=0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{s.server_port}"
    tok = sign_approval(SECRET, "refund", "amount", 40, "alice", session_id="s1")

    def post(key):
        r = urllib.request.Request(base + "/v1/approvals/redeem", json.dumps({"token": tok}).encode(), method="POST",
                                   headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(r).status
        except urllib.error.HTTPError as e:
            return e.code
    assert post(AGENT) == 403  # an agent cannot redeem approvals, even a genuine token
    assert post(APP) == 200
    s.shutdown()


def test_cli_sign_output_verifies(monkeypatch, capsys):
    monkeypatch.setenv("MAYI_SIGNING_SECRET", SECRET)
    assert main(["sign", "refund", "amount", "40", "--by", "alice", "--session", "s1"]) == 0
    a, _ = verify_approval(SECRET, capsys.readouterr().out.strip(), session_id="s1")
    assert a.value == 40
    monkeypatch.delenv("MAYI_SIGNING_SECRET")
    assert main(["sign", "r", "a", "1", "--by", "x"]) == 1
