"""The same end-to-end proxy check as test_mcp_proxy.py, against the 2.x MCP SDK (server and client)."""
import json
import os
import sys

import pytest

pytest.importorskip("mcp")
pytest.importorskip("mcp.server.mcpserver")
anyio = pytest.importorskip("anyio")

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from mayi.signing import sign_approval

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

SERVER = os.path.join(os.path.dirname(__file__), "fixtures", "mcp_demo_server_v2.py")


def test_proxy_with_the_v2_sdk(tmp_path):
    (tmp_path / "catalog.yaml").write_text(CATALOG)
    (tmp_path / "approvals").mkdir()
    log = tmp_path / "calls.jsonl"

    async def main():
        with anyio.fail_after(60):
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "mayi", "mcp-proxy", "--catalog", str(tmp_path / "catalog.yaml"),
                      "--context", json.dumps({"ticket_id": "T-1"}), "--session", "sess",
                      "--approvals-dir", str(tmp_path / "approvals"), "--state-db", str(tmp_path / "state.db"),
                      "--", sys.executable, SERVER],
                env={**os.environ, "MCP_LOG": str(log), "MAYI_SIGNING_SECRET": SECRET})
            async with Client(params) as c:
                tools = {t.name: t for t in (await c.list_tools()).tools}
                assert set(tools) == {"refund", "send_email", "read_inbox"}
                assert "ticket_id" not in tools["refund"].input_schema["properties"]
                ok = await c.call_tool("refund", {"amount": 20, "ticket_id": "T-ATTACKER"})
                assert not ok.is_error and "T-1" in text(ok), text(ok)
                assert (await c.call_tool("refund", {"amount": 900})).is_error
                (tmp_path / "approvals" / "a.token").write_text(sign_approval(SECRET, "refund", "amount", 900, "alice", session_id="sess"))
                assert not (await c.call_tool("refund", {"amount": 900})).is_error
                await c.call_tool("read_inbox", {})
                assert (await c.call_tool("send_email", {"to": "attacker@evil.test", "body": "x"})).is_error
                assert (await c.call_tool("delete_everything", {})).is_error

    anyio.run(main)
    assert not any("attacker" in l or "delete_everything" in l for l in log.read_text().splitlines())
