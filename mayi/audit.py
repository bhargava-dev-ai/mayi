"""
A durable, queryable audit log backed by SQLite. Pass an instance of
SqliteAuditLog as ToolBoundary's audit_log callable, or write your own --
the boundary only needs something callable with one dict argument.

Known fields get their own column so they're queryable with plain SQL.
Anything else (e.g. a field an integration adds) is kept, not dropped, in `extra`.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

_KNOWN = (
    "tool", "outcome", "actor", "catalog_hash", "catalog_version", "catalog_reviewed_by",
    "model_proposed", "pens", "notes", "reason", "escalated_arg", "escalated_value",
)


class SqliteAuditLog:
    def __init__(self, path: str = "mayi_audit.db"):
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                tool TEXT,
                outcome TEXT NOT NULL,
                actor TEXT,
                catalog_hash TEXT,
                catalog_version TEXT,
                catalog_reviewed_by TEXT,
                model_proposed TEXT NOT NULL,
                pens TEXT NOT NULL,
                notes TEXT NOT NULL,
                reason TEXT,
                escalated_arg TEXT,
                escalated_value TEXT,
                extra TEXT NOT NULL
            )
        """)
        self._conn.commit()

    def __call__(self, record: dict) -> None:
        extra = {k: v for k, v in record.items() if k not in _KNOWN}
        self._conn.execute(
            "INSERT INTO audit_log (ts, tool, outcome, actor, catalog_hash, catalog_version, "
            "catalog_reviewed_by, model_proposed, pens, notes, reason, escalated_arg, "
            "escalated_value, extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(), record.get("tool"), record["outcome"], record.get("actor"),
                record.get("catalog_hash"), record.get("catalog_version"), record.get("catalog_reviewed_by"),
                json.dumps(record.get("model_proposed", {})), json.dumps(record.get("pens", {})),
                json.dumps(record.get("notes", {})), record.get("reason"),
                record.get("escalated_arg"), json.dumps(record.get("escalated_value"))
                if "escalated_value" in record else None,
                json.dumps(extra),
            ),
        )
        self._conn.commit()

    _COLUMNS = ("id", "ts", "tool", "outcome", "actor", "catalog_hash", "catalog_version",
                "catalog_reviewed_by", "model_proposed", "pens", "notes", "reason",
                "escalated_arg", "escalated_value", "extra")
    _SELECT_ALL = (
        "SELECT id, ts, tool, outcome, actor, catalog_hash, catalog_version, catalog_reviewed_by, "
        "model_proposed, pens, notes, reason, escalated_arg, escalated_value, extra "
        "FROM audit_log ORDER BY id"
    )

    def all(self) -> list[dict]:
        rows = self._conn.execute(self._SELECT_ALL).fetchall()
        out = []
        for row in rows:
            d = dict(zip(self._COLUMNS, row))
            d["model_proposed"] = json.loads(d["model_proposed"])
            d["pens"] = json.loads(d["pens"])
            d["notes"] = json.loads(d["notes"])
            d["escalated_value"] = json.loads(d["escalated_value"]) if d["escalated_value"] is not None else None
            extra = json.loads(d.pop("extra"))
            d.update(extra)  # extra fields land back at the top level, same as what was logged
            out.append(d)
        return out

    def rejected_and_escalated(self) -> list[dict]:
        return [r for r in self.all() if r["outcome"] != "Executed"]

    def close(self) -> None:
        self._conn.close()
