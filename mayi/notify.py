"""
Tell a person when a call is being held for them.

webhook_notifier(url) returns a function the gateway calls once for each NEW held call.
  - Slack incoming webhooks (hooks.slack.com) get {"text": "..."}; anything else gets JSON.
  - It runs in a background thread with a short timeout. A slow or broken webhook can never
    delay, block or fail a tool call; failures are swallowed (the console still shows the call).
  - Only tool, argument, value and session id are sent: never keys, and never the other
    arguments the model proposed (they may be sensitive).
Only https:// URLs are accepted (plain http only for localhost, for testing).
"""

from __future__ import annotations

import json
import threading
import urllib.request
from typing import Callable
from urllib.parse import urlparse


def _check_url(url: str) -> None:
    u = urlparse(url)
    local = u.hostname in ("127.0.0.1", "localhost", "::1")
    if u.scheme != "https" and not (u.scheme == "http" and local):
        raise ValueError("webhook URL must be https:// (http only for localhost)")


def format_event(event: dict) -> dict:
    return {"event": "held", "session_id": event["session_id"], "tool": event["tool"],
            "arg": event["arg"], "value": event["value"], "reason": event.get("reason", "needs human approval")}


def webhook_notifier(url: str, timeout: float = 5.0, on_error: Callable[[Exception], None] | None = None
                     ) -> Callable[[dict], None]:
    _check_url(url)
    slack = (urlparse(url).hostname or "").endswith("slack.com")

    def send(event: dict) -> None:
        e = format_event(event)
        body = {"text": f"mayi is holding `{e['tool']}` for approval: {e['arg']} = {e['value']!r} "
                        f"(session {e['session_id']})"} if slack else e

        def run() -> None:
            try:
                req = urllib.request.Request(url, json.dumps(body, default=str).encode(), method="POST",
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=timeout).read()  # nosec B310 (https enforced above)
            except Exception as ex:  # never let an alert break a tool call
                if on_error:
                    on_error(ex)
        threading.Thread(target=run, daemon=True).start()

    return send
