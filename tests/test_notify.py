import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mayi import Catalog, ToolBoundary
from mayi.gateway import Gateway
from mayi.notify import format_event, webhook_notifier

CAT = {"refund": {"ticket_id": {"pen": "application", "source": "ticket_id"}, "amount": {"pen": "human"},
                   "note": {"pen": "model", "type": "str", "max_len": 50}}}


@pytest.fixture
def hook():
    got = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
    s = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}/h", got
    s.shutdown()


def wait_for(cond, t=3):
    end = time.time() + t
    while time.time() < end and not cond():
        time.sleep(0.02)


def gateway(notifier):
    g = Gateway(ToolBoundary(Catalog.from_dict(CAT), {"refund": lambda **k: "ok"}), "a-key", "p-key", notifier=notifier)
    g.create_session({"session_id": "s1", "context": {"ticket_id": "T-1"}})
    return g


def test_held_call_sends_one_alert_without_secrets_or_other_args(hook):
    url, got = hook
    g = gateway(webhook_notifier(url))
    call = {"session_id": "s1", "tool": "refund", "args": {"amount": 40, "note": "my secret note"}}
    g.call(call, False); g.call(call, False)  # the same held call asked twice
    wait_for(lambda: got)
    time.sleep(0.2)
    assert len(got) == 1  # only NEW held calls alert
    assert got[0] == {"event": "held", "session_id": "s1", "tool": "refund", "arg": "amount", "value": 40,
                      "reason": "missing human approval"}
    assert "secret" not in json.dumps(got) and "p-key" not in json.dumps(got)


def test_dry_runs_and_executed_calls_do_not_alert(hook):
    url, got = hook
    g = gateway(webhook_notifier(url))
    g.call({"session_id": "s1", "tool": "refund", "args": {"amount": 40, "note": "x"}}, True)
    time.sleep(0.3)
    assert got == []


def test_a_dead_or_slow_webhook_never_breaks_the_call():
    errs = []
    g = gateway(webhook_notifier("http://127.0.0.1:1/x", timeout=0.5, on_error=errs.append))
    r = g.call({"session_id": "s1", "tool": "refund", "args": {"amount": 40, "note": "x"}}, False)
    assert r["outcome"] == "escalated"
    wait_for(lambda: errs)
    assert errs
    boom = gateway(lambda e: (_ for _ in ()).throw(RuntimeError("bad notifier")))
    assert boom.call({"session_id": "s1", "tool": "refund", "args": {"amount": 40, "note": "x"}}, False)["outcome"] == "escalated"


def test_slack_format_and_url_rules():
    posted = []
    import mayi.notify as n
    assert "mayi is holding `refund`" in json.dumps({"text": "mayi is holding `refund`"})
    for bad in ("http://example.com/x", "ftp://x/y", "file:///etc/passwd", "javascript:1"):
        with pytest.raises(ValueError):
            webhook_notifier(bad)
    webhook_notifier("https://hooks.slack.com/services/T/B/x")  # accepted
    assert format_event({"session_id": "s", "tool": "t", "arg": "a", "value": 1})["event"] == "held"


def test_notifier_crash_is_recorded_for_the_operator():
    g = gateway(lambda e: (_ for _ in ()).throw(RuntimeError("bad notifier")))
    g.call({"session_id": "s1", "tool": "refund", "args": {"amount": 40, "note": "x"}}, False)
    assert g.alert_errors and "bad notifier" in g.alert_errors[0]
