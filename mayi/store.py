"""
Durable state for the gateway: sessions, unspent approvals, running budget totals, used
signed-token ids, held calls and untrusted text. Without it all of that lives in memory and a
restart wipes it -- including the totals that stop "many small calls", which an attacker who can
crash the process would love to reset. SQLite, one file, standard library only.

Ordering rules that matter for safety:
  * a single-use approval is marked used in the store BEFORE the tool runs, so a crash after
    the tool ran cannot leave an approval that can be used a second time;
  * a budget amount is reserved BEFORE the tool runs and released if the tool raises, so a
    crash mid-call counts the money as spent (conservative) rather than as unspent.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Optional


class StateStore:
    def __init__(self, path: str = "mayi_state.db"):
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        for ddl in (
            "CREATE TABLE IF NOT EXISTS sessions(sid TEXT PRIMARY KEY, context TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS approvals(id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT NOT NULL, tool TEXT, arg TEXT,"
            " value TEXT, approved_by TEXT, expires_at REAL, scope TEXT, used INTEGER NOT NULL DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS session_tokens(sid TEXT PRIMARY KEY, hash TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS spent(k TEXT PRIMARY KEY, total REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS used_jti(jti TEXT PRIMARY KEY)",
            "CREATE TABLE IF NOT EXISTS pending(k TEXT PRIMARY KEY, body TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS untrusted(id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT NOT NULL, text TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS partial(sid TEXT, tool TEXT, arg TEXT, value TEXT, approver TEXT, PRIMARY KEY(sid, tool, arg, value, approver))",
        ):
            self._db.execute(ddl)

    def _x(self, sql: str, args: tuple = ()):
        with self._lock:
            return self._db.execute(sql, args)

    @staticmethod
    def _j(v: Any) -> str:
        return json.dumps(v, sort_keys=True, default=str)

    # sessions
    def save_session(self, sid: str, ctx: dict) -> None:
        self._x("INSERT OR REPLACE INTO sessions VALUES(?,?)", (sid, self._j(ctx)))

    def load_sessions(self) -> dict[str, dict]:
        return {sid: json.loads(c) for sid, c in self._x("SELECT sid, context FROM sessions")}

    def save_session_token_hash(self, sid: str, h: str) -> None:
        self._x("INSERT OR REPLACE INTO session_tokens VALUES(?,?)", (sid, h))

    def load_session_token_hashes(self) -> dict[str, str]:
        return dict(self._x("SELECT sid, hash FROM session_tokens"))

    # approvals
    def add_approval(self, sid: str, tok: Any) -> int:
        cur = self._x("INSERT INTO approvals(sid,tool,arg,value,approved_by,expires_at,scope) VALUES(?,?,?,?,?,?,?)",
                      (sid, tok.tool, tok.arg, self._j(tok.value), tok.approved_by, tok.expires_at,
                       self._j(tok.scope) if tok.scope else None))
        return cur.lastrowid

    def mark_used(self, approval_id: int) -> None:
        self._x("UPDATE approvals SET used=1 WHERE id=?", (approval_id,))

    def load_approvals(self) -> list[tuple]:
        return list(self._x("SELECT id,sid,tool,arg,value,approved_by,expires_at,scope FROM approvals WHERE used=0"))

    # budgets
    def spent(self, key: tuple) -> float:
        row = self._x("SELECT total FROM spent WHERE k=?", (self._j(list(key)),)).fetchone()
        return row[0] if row else 0.0

    def add_spent(self, key: tuple, delta: float) -> None:
        with self._lock:
            self._db.execute("INSERT INTO spent VALUES(?,?) ON CONFLICT(k) DO UPDATE SET total = total + ?",
                             (self._j(list(key)), delta, delta))

    def clear_spent(self, budget_key: Any) -> None:
        for (k,) in list(self._x("SELECT k FROM spent")):
            if json.loads(k)[0] == budget_key:
                self._x("DELETE FROM spent WHERE k=?", (k,))

    # signed-token replay protection
    def claim_jti(self, jti: str) -> bool:
        """True the first time a token id is claimed, False ever after (atomic)."""
        return self._x("INSERT OR IGNORE INTO used_jti VALUES(?)", (jti,)).rowcount == 1

    def release_jti(self, jti: str) -> None:
        self._x("DELETE FROM used_jti WHERE jti=?", (jti,))

    # held calls
    def save_pending(self, key: str, body: dict) -> None:
        self._x("INSERT OR REPLACE INTO pending VALUES(?,?)", (key, self._j(body)))

    def delete_pending(self, key: str) -> None:
        self._x("DELETE FROM pending WHERE k=?", (key,))

    def load_pending(self) -> dict[str, dict]:
        return {k: json.loads(b) for k, b in self._x("SELECT k, body FROM pending")}

    # untrusted text
    def add_untrusted(self, sid: str, text: str) -> None:
        self._x("INSERT INTO untrusted(sid,text) VALUES(?,?)", (sid, text))
        self._x("DELETE FROM untrusted WHERE sid=? AND id NOT IN (SELECT id FROM untrusted WHERE sid=? ORDER BY id DESC LIMIT 50)", (sid, sid))

    def load_untrusted(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for sid, text in self._x("SELECT sid, text FROM untrusted ORDER BY id"):
            out.setdefault(sid, []).append(text)
        return out

    # two-person approvals in progress
    def add_partial(self, sid: str, tool: str, arg: str, value: Any, approver: str) -> list[str]:
        v = self._j(value)
        self._x("INSERT OR IGNORE INTO partial VALUES(?,?,?,?,?)", (sid, tool, arg, v, approver))
        return [r[0] for r in self._x("SELECT approver FROM partial WHERE sid=? AND tool=? AND arg=? AND value=?", (sid, tool, arg, v))]

    def clear_partial(self, sid: str, tool: str, arg: str, value: Any) -> None:
        self._x("DELETE FROM partial WHERE sid=? AND tool=? AND arg=? AND value=?", (sid, tool, arg, self._j(value)))

    def close(self) -> None:
        with self._lock:
            self._db.close()
