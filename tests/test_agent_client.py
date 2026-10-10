from types import SimpleNamespace as NS
from mayi.agent_client import run_agent, tool_schemas
import mayi.agent_client as ac


def test_schemas_hide_application_args():
    s = tool_schemas({"tools": {"refund": {"ticket_id": "application", "amount": "human",
                                           "note": "model", "total": "derived"}}})
    assert set(s[0]["input_schema"]["properties"]) == {"amount", "note"}


def test_loop_routes_every_call_through_gateway(monkeypatch):
    sent = []
    monkeypatch.setattr(ac, "_post", lambda base, key, path, body: sent.append((key, path, body)) or {"outcome": "escalated"})
    script = [NS(content=[NS(type="tool_use", id="1", name="refund", input={"amount": 9000})]), NS(content=[NS(type="text")])]
    client = NS(messages=NS(create=lambda **kw: script.pop(0)))
    out = run_agent(client, "m", "http://x", "AGENTKEY", "s1", [], "task", log=lambda *a: None)
    assert out == [{"outcome": "escalated"}]
    assert sent == [("AGENTKEY", "/v1/call", {"session_id": "s1", "tool": "refund", "args": {"amount": 9000}})]


def test_waits_for_approval_then_executes_once(monkeypatch):
    calls = []
    answers = iter([{"outcome": "escalated"}, {"outcome": "escalated"}, {"outcome": "simulated"}, {"outcome": "executed"}])
    monkeypatch.setattr(ac, "_post", lambda base, key, path, body: calls.append(path) or next(answers))
    script = [NS(content=[NS(type="tool_use", id="1", name="refund", input={"amount": 40})]), NS(content=[NS(type="text")])]
    client = NS(messages=NS(create=lambda **kw: script.pop(0)))
    out = run_agent(client, "m", "http://x", "K", "s", [], "t", log=lambda *a: None, wait_for_approval=5, poll=0)
    assert out == [{"outcome": "executed"}]
    assert calls == ["/v1/call", "/v1/simulate", "/v1/simulate", "/v1/call"]
