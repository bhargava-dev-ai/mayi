"""
Check a catalog against written-down invariants, and print a counterexample
for any it breaks.

    # invariants.yaml
    no_tool: [delete_database]
    never_model_writable: [send_email.to]
    human_above: {refund.amount: 500}            # no value over 500 runs without a person
    session_total_at_most: {refund.amount: 1000} # standing approvals cannot add up past this
    no_copy_from_untrusted: [send_email.to]
    max_model_text: 500

What this is, exactly: a complete, deterministic analysis of the catalog
*as data*. For every invariant it either shows the catalog cannot break it
or names the argument and a concrete way it can. It does not verify the
Python in boundary.py (that is covered by tests), your tool code, or
anything the model writes inside an allowed free-text field. An invariant
that names a tool or argument that is not in the catalog is an error, not a
pass.
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass
from typing import Any, Optional

from .catalog import ArgSpec, Catalog, Pen

KNOWN = {"no_tool", "never_model_writable", "human_above", "session_total_at_most",
         "no_copy_from_untrusted", "max_model_text", "call_needs_approval", "rate_limited", "two_person_above"}


class InvariantError(ValueError):
    pass


@dataclass
class Finding:
    invariant: str
    subject: str
    ok: bool
    detail: str


def _ref(catalog: Catalog, ref: str) -> tuple[str, str, ArgSpec]:
    if not isinstance(ref, str) or ref.count(".") != 1:
        raise InvariantError(f"{ref!r}: write it as tool.argument")
    tool, arg = ref.split(".")
    if tool not in catalog.tools:
        raise InvariantError(f"{ref}: tool {tool!r} is not in the catalog")
    if arg not in catalog.tools[tool].args:
        raise InvariantError(f"{ref}: argument {arg!r} is not in tool {tool!r}")
    return tool, arg, catalog.tools[tool].args[arg]


def _limit_sup(spec: ArgSpec) -> float:
    """Largest value a standing limit accepts (inf if unbounded)."""
    node = ast.parse(spec.limit, mode="eval").body  # already validated at catalog load
    op, bound = node.ops[0], node.comparators[0].value
    if isinstance(op, (ast.Lt, ast.LtE, ast.Eq)):
        return float(bound)
    return math.inf  # >, >=, != accept arbitrarily large values


def _standing_sup(spec: ArgSpec) -> float:
    """Largest single value that can run with no person involved."""
    if spec.pen != Pen.HUMAN or not spec.limit:
        return -math.inf  # nothing passes on a standing basis
    sup = _limit_sup(spec)
    if spec.session_budget:
        sup = min(sup, float(spec.session_budget))
    return sup


def check(catalog: Catalog, invariants: dict[str, Any]) -> list[Finding]:
    unknown = set(invariants) - KNOWN
    if unknown:
        raise InvariantError(f"unknown invariant(s) {sorted(unknown)}; known: {sorted(KNOWN)}")
    out: list[Finding] = []

    for tool in invariants.get("no_tool", []) or []:
        present = tool in catalog.tools
        out.append(Finding("no_tool", tool, not present,
                           "not in the catalog, so it is rejected" if not present
                           else "is in the catalog, so it can be called"))

    for ref in invariants.get("never_model_writable", []) or []:
        t, a, s = _ref(catalog, ref)
        ok = s.pen != Pen.MODEL
        out.append(Finding("never_model_writable", ref, ok,
                           f"pen is {s.pen.value}" if ok else "pen is model: the model chooses this value"))

    for ref, n in (invariants.get("human_above") or {}).items():
        t, a, s = _ref(catalog, ref)
        if s.pen == Pen.MODEL:
            out.append(Finding("human_above", ref, False, "model pen: any value passes validation"))
            continue
        sup = _standing_sup(s)
        ok = sup <= n
        if ok:
            how = "every value needs a person" if sup == -math.inf else f"standing approval stops at {sup:g}"
            out.append(Finding("human_above", ref, True, how))
        else:
            ex = "an arbitrarily large value" if sup == math.inf else f"{sup:g}"
            out.append(Finding("human_above", ref, False,
                               f"counterexample: {ex} runs on standing approval, over {n:g}"))

    for ref, n in (invariants.get("session_total_at_most") or {}).items():
        t, a, s = _ref(catalog, ref)
        if s.pen == Pen.MODEL:
            out.append(Finding("session_total_at_most", ref, False, "model pen: no cap on the total"))
            continue
        if s.pen != Pen.HUMAN or not s.limit:
            out.append(Finding("session_total_at_most", ref, True,
                               "no standing approval, so a person approves every amount"))
        elif s.session_budget and s.session_budget <= n:
            out.append(Finding("session_total_at_most", ref, True,
                               f"session_budget {s.session_budget:g} caps standing approvals"))
        else:
            out.append(Finding("session_total_at_most", ref, False,
                               "counterexample: repeated calls each within the limit add up without bound"
                               if not s.session_budget else
                               f"session_budget {s.session_budget:g} is over {n:g}"))

    for tool in invariants.get("call_needs_approval", []) or []:
        if tool not in catalog.tools:
            raise InvariantError(f"{tool!r} is not in the catalog")
        ts = catalog.tools[tool]
        gated = ts.requires_approval or any(s.pen == Pen.HUMAN and not s.limit for s in ts.args.values())
        out.append(Finding("call_needs_approval", tool, gated,
                           "a person must approve every call" if gated else
                           "counterexample: a call can run with no person involved"))

    for tool in invariants.get("rate_limited", []) or []:
        if tool not in catalog.tools:
            raise InvariantError(f"{tool!r} is not in the catalog")
        rl = catalog.tools[tool].rate_limit
        out.append(Finding("rate_limited", tool, rl is not None,
                           f"at most {rl[0]} calls per {rl[1]:g}s per session" if rl else "no _rate_limit: calls are unlimited"))

    for ref, n in (invariants.get("two_person_above") or {}).items():
        t, a, s = _ref(catalog, ref)
        sup = _standing_sup(s) if s.pen == Pen.HUMAN else math.inf
        ok = (s.pen == Pen.HUMAN and s.dual_control_above is not None and s.dual_control_above <= n and sup <= s.dual_control_above)
        out.append(Finding("two_person_above", ref, ok,
                           f"values over {s.dual_control_above:g} need two different approvers" if ok else
                           "counterexample: a value over %g can run with one approver or none" % n))

    for ref in invariants.get("no_copy_from_untrusted", []) or []:
        t, a, s = _ref(catalog, ref)
        ok = s.pen != Pen.MODEL or bool(s.untrusted_copy)
        out.append(Finding("no_copy_from_untrusted", ref, ok,
                           "copied values are held or rejected" if s.pen == Pen.MODEL and ok
                           else f"model pen with no untrusted_copy check" if not ok else f"pen is {s.pen.value}"))

    cap: Optional[int] = invariants.get("max_model_text")
    if cap is not None:
        for tn, ts in catalog.tools.items():
            for an, s in ts.args.items():
                if s.pen == Pen.MODEL and (s.type in (None, "str")):
                    ok = s.max_len is not None and s.max_len <= cap
                    out.append(Finding("max_model_text", f"{tn}.{an}", ok,
                                       f"max_len {s.max_len}" if ok else
                                       (f"max_len {s.max_len} is over {cap}" if s.max_len else "no max_len: unbounded text")))
    return out


def render(findings: list[Finding]) -> str:
    lines = [f"{'PASS' if f.ok else 'FAIL'}  {f.invariant}: {f.subject} -- {f.detail}" for f in findings]
    bad = sum(not f.ok for f in findings)
    lines.append("")
    lines.append(f"{len(findings) - bad} of {len(findings)} hold." if bad else f"All {len(findings)} hold.")
    return "\n".join(lines)
