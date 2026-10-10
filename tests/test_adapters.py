import asyncio
import json
import threading

import pytest

from mayi import Catalog, ToolBoundary
from mayi.adapters import crewai_tools, openai_agents_tools, outcome_text
from mayi.client import MayiClient
from mayi.gateway import Gateway, serve

SCHEMAS = [{"name": "refund", "description": "refund", "input_schema": {
    "type": "object", "properties": {"amount": {"description": "a"}, "note": {"description": "n"}},
    "required": ["amount", "note"]}}]


@pytest.fixture
def stack():
    cat = Catalog.from_dict({"refund": {
        "ticket_id": {"pen": "application", "source": "ticket_id"},
        "amount": {"pen": "human", "limit": "amount <= 50"},
        "note": {"pen": "model", "type": "str", "max_len": 40}}})
    ran = []
    gw = Gateway(ToolBoundary(cat, {"refund": lambda **kw: ran.append(kw) or {"refunded": kw["amount"]}}), "AG", "AP")
    srv = serve(gw, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    gw.create_session({"session_id": "s", "context": {"ticket_id": "T-1"}})
    yield MayiClient(f"http://127.0.0.1:{srv.server_port}", "AG"), ran
    srv.shutdown()


def check(run, ran):
    ok, held = json.loads(run({"amount": 20, "note": "late"})), json.loads(run({"amount": 9000, "note": "x"}))
    assert ok["status"] == "done" and held["status"].startswith("waiting")
    assert ran == [{"ticket_id": "T-1", "amount": 20, "note": "late"}]  # the held call never ran


def test_openai_agents_adapter(stack):
    pytest.importorskip("agents")
    client, ran = stack
    t = openai_agents_tools(client, "s", SCHEMAS)[0]
    check(lambda a: asyncio.run(t.on_invoke_tool(None, json.dumps(a))), ran)
    assert json.loads(asyncio.run(t.on_invoke_tool(None, "not json")))["status"] == "not allowed"


def test_crewai_adapter(stack):
    pytest.importorskip("crewai")
    client, ran = stack
    t = crewai_tools(client, "s", SCHEMAS)[0]
    check(lambda a: t.run(**a), ran)


def test_outcome_text_shapes():
    assert json.loads(outcome_text({"outcome": "rejected", "reason": "no"}))["status"] == "not allowed"
