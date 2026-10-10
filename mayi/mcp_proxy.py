"""
mayi in front of any MCP server (stdio). The agent host launches mayi instead of the server:

    mayi mcp-proxy --catalog catalog.yaml --context '{"customer_email": "a@b.test"}' -- npx some-mcp-server

What the proxy does, on the wire (newline-delimited JSON-RPC):
  * tools/list   answers are filtered to the tools in the catalog, and arguments that the
                 application controls are hidden from the model's view of each tool
  * tools/call   goes through the same ToolBoundary the gateway uses: application values replace
                 the model's, limits/budgets/rules apply, held calls are NOT forwarded
  * everything else is passed through unchanged
Text returned by upstream tools is remembered as untrusted, so `untrusted_copy` works without the
host having to report anything.

Approving a held call: the proxy writes a line to stderr and a file in --pending-dir. A person mints
a signed token (`mayi sign ...`) and drops it in --approvals-dir; the proxy reads it on the next call.
Tokens are checked like the gateway's (signature, expiry, session, one use).

Limits: stdio only; JSON-RPC batches are refused; a single upstream tool failure is returned as an
MCP error result, not retried.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess  # nosec B404 (the operator names the upstream command)
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from .approvals import ApprovalSet, ApprovalToken
from .boundary import ToolBoundary
from .catalog import Catalog, Pen
from .results import Escalated, Executed, Rejected
from .signing import InvalidApproval, token_alg, verify_approval, verify_approval_ed25519


class UpstreamError(RuntimeError):
    pass


class McpProxy:
    def __init__(self, catalog: Catalog, upstream_cmd: list[str], context: dict, session_id: str = "mcp-session",
                 approvals_dir: Optional[str] = None, pending_dir: Optional[str] = None,
                 signing_secret: Optional[str] = None, public_key: Optional[str] = None,
                 state: Any = None, audit_log=None, stdin=None, stdout=None, stderr=None):
        self.catalog, self.context, self.sid = catalog, context, session_id
        self.approvals_dir, self.pending_dir = approvals_dir, pending_dir
        self._secret, self._pub, self._state = signing_secret, public_key, state
        self._in, self._out, self._err = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
        self._wlock = threading.Lock()   # writes to the client
        self._ulock = threading.Lock()   # writes to the upstream server
        self._list_ids: set = set()
        self._ids = itertools.count(1)
        self._waiting: dict[str, tuple[threading.Event, list]] = {}
        self._untrusted: list[str] = []
        self._tl = threading.local()  # the client's request metadata for the call being handled
        self._approvals = ApprovalSet(on_use=self._on_use)
        self._seen_jti: set[str] = set()
        self._tools_seen: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=8)
        self._proc = subprocess.Popen(upstream_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,  # nosec B603
                                      stderr=None, text=True, bufsize=1)
        tools = {n: self._make_forwarder(n) for n in catalog.tools}
        self.boundary = ToolBoundary(catalog, tools, audit_log=audit_log, state=state, strict=True)

    # ------------------------------------------------------------------ plumbing
    def _on_use(self, tok: ApprovalToken) -> None:
        if self._state is not None and tok.store_id is not None:
            self._state.mark_used(tok.store_id)

    def _send_client(self, msg: dict) -> None:
        with self._wlock:
            self._out.write(json.dumps(msg) + "\n")
            self._out.flush()

    def _send_upstream(self, msg: dict) -> None:
        with self._ulock:
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()

    def _make_forwarder(self, name: str):
        def forward(**args: Any) -> dict:
            rid = f"mayi-{next(self._ids)}"
            ev: tuple[threading.Event, list] = (threading.Event(), [])
            self._waiting[rid] = ev
            params: dict = {"name": name, "arguments": args}
            meta = getattr(self._tl, "meta", None)
            if meta is not None:  # newer MCP revisions require per-request metadata on every call; reuse the client's
                params["_meta"] = meta
            self._send_upstream({"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": params})
            if not ev[0].wait(timeout=300):
                self._waiting.pop(rid, None)
                raise UpstreamError("upstream tool timed out")
            resp = ev[1][0]
            if "error" in resp:
                raise UpstreamError(str(resp["error"].get("message", "upstream error")))
            result = resp.get("result", {})
            for block in result.get("content", []) if isinstance(result, dict) else []:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    self._untrusted.append(block["text"][:200_000])
            del self._untrusted[:-50]
            return result
        return forward

    # ------------------------------------------------------------------ approvals
    def _ingest_tokens(self) -> None:
        if not self.approvals_dir or not os.path.isdir(self.approvals_dir):
            return
        for fn in sorted(os.listdir(self.approvals_dir)):
            path = os.path.join(self.approvals_dir, fn)
            if not os.path.isfile(path):
                continue
            try:
                tok = open(path).read().strip()
                if token_alg(tok) == "ed25519" and self._pub:
                    approval, jti = verify_approval_ed25519(self._pub, tok, session_id=self.sid)
                elif token_alg(tok) == "hmac" and self._secret:
                    approval, jti = verify_approval(self._secret, tok, session_id=self.sid)
                else:
                    continue
            except (InvalidApproval, OSError, RuntimeError, ValueError):
                continue
            fresh = self._state.claim_jti(jti) if self._state is not None else (jti not in self._seen_jti)
            self._seen_jti.add(jti)
            if fresh:
                if self._state is not None:
                    approval.store_id = self._state.add_approval(self.sid, approval)
                self._approvals.add(approval)
            try:
                os.remove(path)  # a token file is spent the moment it is read
            except OSError:
                pass

    def _write_pending(self, esc: Escalated) -> None:
        msg = f"mayi: held {esc.tool}.{esc.arg} = {json.dumps(esc.proposed_value)} ({esc.reason}). " \
              f"Approve: mayi sign {esc.tool} {esc.arg} {json.dumps(json.dumps(esc.proposed_value))} --by YOU --session {self.sid} > <approvals-dir>/ok.token"
        print(msg, file=self._err, flush=True)
        if self.pending_dir:
            os.makedirs(self.pending_dir, exist_ok=True)
            with open(os.path.join(self.pending_dir, f"{esc.tool}.{esc.arg}.json"), "w") as f:
                json.dump({"session_id": self.sid, "tool": esc.tool, "arg": esc.arg, "value": esc.proposed_value}, f)

    # ------------------------------------------------------------------ messages
    def _handle_call(self, msg: dict) -> None:
        params = msg.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return self._send_client({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32602, "message": "bad tools/call params"}})
        self._ingest_tokens()
        self._tl.meta = params.get("_meta")
        try:
            res = self.boundary.call(name, args, self.context, approvals=self._approvals, budget_key=self.sid,
                                     untrusted=list(self._untrusted))
        except UpstreamError as e:
            return self._reply_text(msg["id"], f"mayi: the tool failed: {e}", error=True)
        except Exception:  # noqa: BLE001 -- never leak internals to the model
            return self._reply_text(msg["id"], "mayi: internal error; the call was not completed", error=True)
        if isinstance(res, Executed):
            return self._send_client({"jsonrpc": "2.0", "id": msg["id"], "result": res.result})
        if isinstance(res, Escalated):
            self._write_pending(res)
            return self._reply_text(msg["id"], f"Not run yet: a person must approve {res.tool}.{res.arg}. Tell the user it is waiting for approval; do not retry with a different value.", error=True)
        self._reply_text(msg["id"], f"Not allowed: {res.reason}", error=True)

    def _reply_text(self, rid: Any, text: str, error: bool) -> None:
        self._send_client({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}], "isError": error,
                                                                       "resultType": "complete"}})  # 2.x clients require it; 1.x ignores it

    def _filter_tools(self, result: dict) -> dict:
        out = []
        for t in result.get("tools", []):
            spec = self.catalog.tools.get(t.get("name"))
            if spec is None:
                continue  # not in the catalog: the model never hears of it
            t = json.loads(json.dumps(t))
            hidden = {a for a, s in spec.args.items() if s.pen in (Pen.APPLICATION, Pen.DERIVED)}
            schema = t.get("inputSchema") or {}
            for h in hidden:
                (schema.get("properties") or {}).pop(h, None)
            if isinstance(schema.get("required"), list):
                schema["required"] = [r for r in schema["required"] if r not in hidden]
            out.append(t)
            self._tools_seen.add(t["name"])
        return {**result, "tools": out}

    def _client_loop(self) -> None:
        for line in self._in:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, list):
                self._send_client({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "batches are not supported by mayi"}})
            elif isinstance(msg, dict) and msg.get("method") == "tools/call" and "id" in msg:
                self._pool.submit(self._handle_call, msg)
            elif isinstance(msg, dict):
                if msg.get("method") == "tools/list" and "id" in msg:
                    self._list_ids.add(msg["id"])
                self._send_upstream(msg)
        try:
            self._proc.stdin.close()
        except OSError:
            pass

    def _upstream_loop(self) -> None:
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("id") in self._waiting:
                ev = self._waiting.pop(msg["id"])
                ev[1].append(msg)
                ev[0].set()
            elif isinstance(msg, dict) and "result" in msg and msg.get("id") in self._list_ids:
                self._list_ids.discard(msg["id"])
                msg["result"] = self._filter_tools(msg["result"])
                self._send_client(msg)
            else:
                self._send_client(msg)

    def run(self) -> int:
        t = threading.Thread(target=self._upstream_loop, daemon=True)
        t.start()
        self._client_loop()
        t.join(timeout=5)
        self._pool.shutdown(wait=True)
        try:
            self._proc.terminate()
        except OSError:
            pass
        return 0
