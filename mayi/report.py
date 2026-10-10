"""
A governance report over mayi's own audit trail: what the catalog says
(version, owner, reviewer) and what actually happened (how many calls,
how many were blocked or escalated, who approved what, who the actors
were). This is the artifact a compliance or risk reviewer asks for --
built from the same audit rows the boundary already writes, not a
separate system that can drift out of sync with reality.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from .catalog import Catalog


@dataclass
class GovernanceReport:
    catalog_version: Optional[str]
    catalog_owner: Optional[str]
    catalog_reviewed_by: Optional[str]
    catalog_reviewed_on: Optional[str]
    total_calls: int
    executed: int
    rejected: int
    escalated: int
    by_tool: dict[str, int] = field(default_factory=dict)
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    actors: dict[str, int] = field(default_factory=dict)
    approvers_seen: dict[str, int] = field(default_factory=dict)
    distinct_catalog_hashes: int = 0

    def as_text(self) -> str:
        lines = [
            "mayi governance report",
            "=======================",
            "",
            "Policy",
            f"  version:      {self.catalog_version or '(not set)'}",
            f"  owner:        {self.catalog_owner or '(not set)'}",
            f"  reviewed by:  {self.catalog_reviewed_by or '(not set)'}",
            f"  reviewed on:  {self.catalog_reviewed_on or '(not set)'}",
            "",
            "Activity",
            f"  total calls:  {self.total_calls}",
            f"  executed:     {self.executed}",
            f"  rejected:     {self.rejected}",
            f"  escalated:    {self.escalated}",
            f"  distinct catalog versions seen in the log: {self.distinct_catalog_hashes}",
            "",
            "By tool:",
        ]
        for tool, n in sorted(self.by_tool.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {tool}: {n}")
        if self.rejection_reasons:
            lines.append("")
            lines.append("Top rejection reasons:")
            for reason, n in Counter(self.rejection_reasons).most_common(5):
                lines.append(f"  ({n}x) {reason}")
        if self.approvers_seen:
            lines.append("")
            lines.append("Approvers seen in this period:")
            for approver, n in sorted(self.approvers_seen.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {approver}: {n} approval(s)")
        if self.actors:
            lines.append("")
            lines.append("Actors:")
            for actor, n in sorted(self.actors.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {actor}: {n} call(s)")
        return "\n".join(lines)


def generate_report(records: list[dict], catalog: Catalog) -> GovernanceReport:
    """
    records: the list of audit rows, e.g. from SqliteAuditLog.all(), or
    any list of dicts shaped like the records the boundary logs.
    """
    by_tool: Counter = Counter()
    rejection_reasons: Counter = Counter()
    actors: Counter = Counter()
    approvers: Counter = Counter()
    hashes = set()
    executed = rejected = escalated = 0

    for r in records:
        outcome = r.get("outcome", "")
        if outcome not in ("Executed", "Rejected", "Escalated"):
            continue  # correlation or other non-decision rows
        by_tool[r.get("tool", "?")] += 1
        if r.get("catalog_hash"):
            hashes.add(r["catalog_hash"])
        if r.get("actor"):
            actors[r["actor"]] += 1
        if outcome == "Executed":
            executed += 1
        elif outcome == "Rejected":
            rejected += 1
            if r.get("reason"):
                rejection_reasons[r["reason"]] += 1
        elif outcome == "Escalated":
            escalated += 1
        for note in (r.get("notes") or {}).values():
            if isinstance(note, str) and note.startswith("approved by "):
                approvers[note[len("approved by "):]] += 1

    return GovernanceReport(
        catalog_version=catalog.version, catalog_owner=catalog.owner,
        catalog_reviewed_by=catalog.reviewed_by, catalog_reviewed_on=catalog.reviewed_on,
        total_calls=executed + rejected + escalated,
        executed=executed, rejected=rejected, escalated=escalated,
        by_tool=dict(by_tool), rejection_reasons=dict(rejection_reasons),
        actors=dict(actors), approvers_seen=dict(approvers),
        distinct_catalog_hashes=len(hashes),
    )
