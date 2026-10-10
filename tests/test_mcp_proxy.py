import json
import os
import sys

import pytest

pytest.importorskip("mcp")
pytest.importorskip("mcp.server.fastmcp")  # the fixture server uses the 1.x SDK API
anyio = pytest.importorskip("anyio")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mayi.signing import sign_approval

HERE = os.path.dirname(__file__)
SERVER = os.path.join(HERE, "fixtures", "mcp_demo_server.py")
CATALOG = """
refund:
  ticket_id: {pen: application, source: ticket_id}
  amount: {pen: human, limit: "amount <= 50"}
send_email:
  to: {pen: model, type: str, max_len: 80, untrusted_copy: hold}
  body: {pen: model, type: str, max_len: 300}
read_inbox: {}
"""
SECRET = "m" * 32


def text(res):
    return "".join(getattr(c, "text", "") for c in res.content)


def test_proxy_end_to_end_with_the_official_mcp_client(tmp_path):
    (tmp_path / "catalog.yaml").write_text(CATALOG)
    (tmp_path / "approvals").mkdir()
    log = tmp_path / "calls.jsonl"

    async def main():
        with anyio.fail_after(60):
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "mayi", "mcp-proxy", "--catalog", str(tmp_path / "catalog.yaml"),
                      "--context", json.dumps({"ticket_id": "T-1"}), "--session", "sess",
                      "--approvals-dir", str(tmp_path / "approvals"), "--pending-dir", str(tmp_path / "pending"),
                      "--state-db", str(tmp_path / "state.db"), "--", sys.executable, SERVER],
                env={**os.environ, "MCP_LOG": str(log), "MAYI_SIGNING_SECRET": SECRET})
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    tools = {t.name: t for t in (await s.list_tools()).tools}
                    assert set(tools) == {"refund", "send_email", "read_inbox"}            # delete_everything is invisible
                    assert "ticket_id" not in tools["refund"].inputSchema["properties"]   # the app's argument is hidden
                    assert "amount" in tools["refund"].inputSchema["properties"]

                    ok = await s.call_tool("refund", {"amount": 20, "ticket_id": "T-ATTACKER"})
                    assert not ok.isError and "T-1" in text(ok) and "ATTACKER" not in text(ok)   # app value replaced the model's

                    held = await s.call_tool("refund", {"amount": 900})
                    assert held.isError and "Not run yet" in text(held)
                    assert (await s.call_tool("delete_everything", {})).isError
                    (tmp_path / "approvals" / "a.token").write_text(sign_approval(SECRET, "refund", "amount", 900, "alice", session_id="sess"))
                    done = await s.call_tool("refund", {"amount": 900})
                    assert not done.isError and "T-1" in text(done)
                    assert (await s.call_tool("refund", {"amount": 900})).isError                  # single use
                    assert not (tmp_path / "approvals" / "a.token").exists()                       # token file is spent

                    await s.call_tool("read_inbox", {})                                           # its text is now untrusted
                    bad = await s.call_tool("send_email", {"to": "attacker@evil.test", "body": "history"})
                    assert bad.isError and "Not run yet" in text(bad)
                    good = await s.call_tool("send_email", {"to": "jane@ok.test", "body": "sorry"})
                    assert not good.isError

    anyio.run(main)
    ran = [json.loads(l) for l in log.read_text().splitlines()]
    assert [c["tool"] for c in ran] == ["refund", "refund", "read_inbox", "send_email"]
    assert ran[0]["args"] == {"ticket_id": "T-1", "amount": 20}
    assert not any(c["tool"] == "delete_everything" for c in ran)
    assert not any("attacker" in json.dumps(c) for c in ran)
