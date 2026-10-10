"""
Compare two catalogs (policy versions): which tools and arguments changed,
and which changes loosen authority. Deterministic and read-only -- meant for
review before a new policy version ships, and for audit ("what changed
between v12 and v13?"). Pair with ToolBoundary.simulate() to replay recorded
calls under each version.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import Catalog, Pen

# How much freedom the model has over an argument, lowest = most restricted.
_LOOSENESS = {Pen.HUMAN: 0, Pen.APPLICATION: 1, Pen.DERIVED: 1, Pen.MODEL: 2}
_FIELDS = ("source", "max_len", "allowlist", "type", "limit", "session_budget", "untrusted_copy",
           "allowlist_from", "forbid", "dual_control_above")


@dataclass
class CatalogDiff:
    changes: list[str] = field(default_factory=list)
    loosened: list[str] = field(default_factory=list)  # subset that gives the model more authority

    def __bool__(self) -> bool:
        return bool(self.changes)

    def as_text(self) -> str:
        if not self.changes:
            return "No differences."
        lines = [f"- {c}" for c in self.changes]
        if self.loosened:
            lines += ["", "Loosens authority (review first):"] + [f"  ! {c}" for c in self.loosened]
        return "\n".join(lines)


def diff_catalogs(old: Catalog, new: Catalog) -> CatalogDiff:
    d = CatalogDiff()
    for tool in sorted(set(old.tools) | set(new.tools)):
        if tool not in new.tools:
            d.changes.append(f"tool removed: {tool}")
            continue
        if tool not in old.tools:
            d.changes.append(f"tool added: {tool}")
            continue
        if old.tools[tool].requires_approval != new.tools[tool].requires_approval:
            on = new.tools[tool].requires_approval
            d.changes.append(f"tool-level approval {'added' if on else 'removed'}: {tool}")
            if not on:
                d.loosened.append(f"tool-level approval removed: {tool}")
        ot, nt = old.tools[tool], new.tools[tool]
        if ot.rate_limit != nt.rate_limit:
            d.changes.append(f"rate limit changed: {tool} {ot.rate_limit!r} -> {nt.rate_limit!r}")
            if nt.rate_limit is None or (ot.rate_limit and nt.rate_limit[0] / nt.rate_limit[1] > ot.rate_limit[0] / ot.rate_limit[1]):
                d.loosened.append(f"rate limit {'removed' if nt.rate_limit is None else 'raised'}: {tool}")
        old_rules, new_rules = [r[0] for r in ot.require], [r[0] for r in nt.require]
        if old_rules != new_rules:
            d.changes.append(f"argument rules changed: {tool} {old_rules} -> {new_rules}")
            for r in set(old_rules) - set(new_rules):
                d.loosened.append(f"argument rule removed or changed: {tool}: {r}")
        o, n = old.tools[tool].args, new.tools[tool].args
        for arg in sorted(set(o) | set(n)):
            label = f"{tool}.{arg}"
            if arg not in n:
                d.changes.append(f"argument removed: {label}")
            elif arg not in o:
                msg = f"argument added: {label} ({n[arg].pen.value})"
                d.changes.append(msg)
                if n[arg].pen == Pen.MODEL:
                    d.loosened.append(msg)
            else:
                if o[arg].pen != n[arg].pen:
                    msg = f"pen changed: {label} {o[arg].pen.value} -> {n[arg].pen.value}"
                    d.changes.append(msg)
                    if _LOOSENESS[n[arg].pen] > _LOOSENESS[o[arg].pen]:
                        d.loosened.append(msg)
                for f in _FIELDS:
                    a, b = getattr(o[arg], f), getattr(n[arg], f)
                    if a != b:
                        d.changes.append(f"{f} changed: {label} {a!r} -> {b!r}")
                        if f == "limit" and b is not None and a is None:
                            d.loosened.append(f"standing limit added: {label} {b!r}")
                        if f == "untrusted_copy" and (b is None or (a == "reject" and b == "hold")):
                            d.loosened.append(f"untrusted-copy check {'removed' if b is None else 'relaxed'}: {label} {a!r} -> {b!r}")
                        if f == "allowlist_from" and b is None:
                            d.loosened.append(f"session allowlist removed: {label}")
                        if f == "forbid" and (set(a or []) - set(b or [])):
                            d.loosened.append(f"forbidden content narrowed: {label} {a!r} -> {b!r}")
                        if f == "dual_control_above" and a is not None and (b is None or b > a):
                            d.loosened.append(f"two-person threshold {'removed' if b is None else 'raised'}: {label} {a!r} -> {b!r}")
                        if f == "session_budget" and (b is None or (a is not None and b > a)):
                            d.loosened.append(f"session budget {'removed' if b is None else 'raised'}: {label} {a!r} -> {b!r}")
    return d
