"""
An approval token covers one tool, one argument, one value. It is never
a standing "whatever it needs" grant — a bounded standing approval is
expressed in the catalog's `limit` instead (see limits.py), reviewed and
committed once, not minted per call.

Tokens are single-use and may carry an expiry. A token that already fired,
or that has expired, is exactly as absent as one that was never issued.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ApprovalToken:
    tool: str
    arg: str
    value: Any
    approved_by: str
    expires_at: Optional[float] = None  # epoch seconds; None = no expiry
    used: bool = False
    # Optional resource scope, e.g. {"ticket_id": "T-9"}: the approval holds
    # only for a call whose application-pen (trusted) argument values equal
    # these. Keys are checked against values the application resolved, never
    # against anything the model proposed. A scoped token with no matching
    # trusted context is exactly as absent as no token (fails closed).
    scope: Optional[dict] = None
    approvers: tuple = ()  # for two-person approvals: every distinct person who signed off
    store_id: Optional[int] = None  # row id when a StateStore keeps this token

    def matches(self, tool: str, arg: str, value: Any, context: Optional[dict] = None) -> bool:
        if not (self.tool == tool and self.arg == arg and self.value == value):
            return False
        if self.scope:
            context = context or {}
            return all(k in context and context[k] == v for k, v in self.scope.items())
        return True

    def is_live(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return not self.used and (self.expires_at is None or now <= self.expires_at)


class ApprovalSet:
    """A small collection of tokens gathered for one call attempt."""

    def __init__(self, tokens: Optional[list[ApprovalToken]] = None, on_use: Optional[Any] = None):
        self._tokens = tokens or []
        self.on_use = on_use  # called with the token the moment it is consumed, before the tool runs

    def add(self, token: "ApprovalToken") -> None:
        self._tokens.append(token)

    def take(self, tool: str, arg: str, value: Any, now: Optional[float] = None,
             context: Optional[dict] = None) -> Optional[ApprovalToken]:
        """
        Find a live token matching exactly this tool/arg/value and consume
        it. Returns None if no such token exists, is expired, or was
        already used by an earlier call in this same run.
        """
        for token in self._tokens:
            if token.matches(tool, arg, value, context) and token.is_live(now):
                token.used = True
                if self.on_use is not None:
                    self.on_use(token)
                return token
        return None

    def peek(self, tool: str, arg: str, value: Any, now: Optional[float] = None,
             context: Optional[dict] = None) -> Optional[ApprovalToken]:
        """Like take(), but does not consume the token (used by dry runs)."""
        for token in self._tokens:
            if token.matches(tool, arg, value, context) and token.is_live(now):
                return token
        return None
