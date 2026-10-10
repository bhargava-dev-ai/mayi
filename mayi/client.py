"""
Small client for a mayi gateway. Adopting mayi from an agent is two settings:

    MAYI_BASE_URL=http://127.0.0.1:8787     MAYI_API_KEY=<agent key>

    from mayi.client import MayiClient
    mayi = MayiClient()
    mayi.decide("ticket-1042", "refund", {"amount": 40, "note": "late"})   # allow / hold / reject
    mayi.call("ticket-1042", "refund", {"amount": 40, "note": "late"})     # runs it if allowed

Uses only the agent key. It cannot create sessions or approve anything.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Optional


class MayiClient:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None, timeout: float = 30.0):
        self.base_url = (base_url or os.environ.get("MAYI_BASE_URL") or "http://127.0.0.1:8787").rstrip("/")
        self.api_key = api_key or os.environ.get("MAYI_API_KEY")
        if not self.api_key:
            raise ValueError("set MAYI_API_KEY (the agent key) or pass api_key=")
        self.timeout = timeout
        self._tokens: dict[str, str] = {}  # session id -> session token

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + path, json.dumps(body).encode(), method="POST",
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:  # nosec B310 (operator-configured URL)
            return json.loads(r.read())

    def bind(self, session_id: str, session_token: str) -> None:
        """Remember the token the application got when it created this session."""
        self._tokens[session_id] = session_token

    def _body(self, session_id: str, tool: str, args: dict, actor: Optional[str]) -> dict:
        b: dict[str, Any] = {"session_id": session_id, "tool": tool, "args": args}
        if session_id in self._tokens:
            b["session_token"] = self._tokens[session_id]
        if actor:
            b["actor"] = actor
        return b

    def decide(self, session_id: str, tool: str, args: dict, actor: Optional[str] = None) -> dict:
        """Dry run. Returns {"decision": "allow"|"hold"|"reject", "reason": ...}. Nothing executes."""
        return self._post("/v1/decide", self._body(session_id, tool, args, actor))

    def simulate(self, session_id: str, tool: str, args: dict, actor: Optional[str] = None) -> dict:
        return self._post("/v1/simulate", self._body(session_id, tool, args, actor))

    def call(self, session_id: str, tool: str, args: dict, actor: Optional[str] = None) -> dict:
        """Runs the tool through the gateway if (and only if) the rules allow it."""
        return self._post("/v1/call", self._body(session_id, tool, args, actor))

    def add_untrusted(self, session_id: str, text: str) -> dict:
        """Tell the gateway the model has read this untrusted text (an email, a web page). Values the
        model later copies verbatim from it into arguments marked `untrusted_copy` are held or rejected."""
        body = {"session_id": session_id, "text": text}
        if session_id in self._tokens:
            body["session_token"] = self._tokens[session_id]
        return self._post("/v1/untrusted", body)
