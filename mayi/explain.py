"""
Explain a catalog in plain words, straight from the file. Nothing here is estimated: every
line is read off the catalog, and the warnings are fixed rules about settings that are
usually mistakes. It cannot tell you whether a tool is dangerous, only who controls what.
"""

from __future__ import annotations

import re
from typing import Any

from .catalog import Catalog, Pen

_OUTBOUND = re.compile(r"(send|email|mail|post|publish|notify|message|reply|sms|tweet|webhook|upload|forward)", re.I)


def explain_catalog(catalog: Catalog) -> dict[str, Any]:
    tools: dict[str, Any] = {}
    warnings: list[str] = []
    for tname, spec in catalog.tools.items():
        t: dict[str, Any] = {"application_fixed": {}, "model_writable": {}, "human_gated": {},
                             "standing_approvals": {}, "derived": [], "worst_case_without_a_human": []}
        for a, s in spec.args.items():
            if s.pen == Pen.APPLICATION:
                t["application_fixed"][a] = s.source
            elif s.pen == Pen.DERIVED:
                t["derived"].append(a)
            elif s.pen == Pen.MODEL:
                c = {k: v for k, v in (("type", s.type), ("max_len", s.max_len), ("allowlist", s.allowlist)) if v is not None}
                t["model_writable"][a] = c
                if s.type in (None, "str") and s.max_len is None and s.fields is None:
                    warnings.append(f"{tname}.{a}: the model writes free text with no max_len (unbounded length)")
            else:
                if s.limit:
                    t["standing_approvals"][a] = {"limit": s.limit, "session_budget": s.session_budget}
                    if s.session_budget:
                        t["worst_case_without_a_human"].append(
                            f"{a}: at most {s.session_budget:g} in total per session (each call {s.limit})")
                    else:
                        t["worst_case_without_a_human"].append(
                            f"{a}: each call {s.limit}, but there is NO total, so repeated calls add up without limit")
                        warnings.append(f"{tname}.{a}: standing limit '{s.limit}' has no session_budget, so many small calls can exceed it")
                else:
                    t["human_gated"][a] = "a person approves every call"
        t["always_needs_a_person"] = spec.requires_approval
        t["rate_limit"] = spec.rate_limit
        t["rules"] = [r[0] for r in spec.require]
        if not spec.args and not spec.requires_approval:
            warnings.append(f"{tname}: takes no arguments and is not marked _requires_approval, so mayi has nothing to gate: it runs whenever it is called")
        if not (spec.args or spec.requires_approval):
            tools[tname] = t
            continue
        if not spec.requires_approval and not (t["application_fixed"] or t["human_gated"] or t["standing_approvals"] or t["derived"]):
            warnings.append(f"{tname}: every argument is model-writable; nothing in this tool is controlled by your app or a person")
        text_args = [a for a, c in t["model_writable"].items() if c.get("type") in (None, "str")]
        if _OUTBOUND.search(tname) and text_args:
            warnings.append(f"{tname}: looks like it sends something outward, and the model writes its text ({', '.join(text_args)}); "
                            "mayi bounds the length but does not judge the content")
        tools[tname] = t
    return {"tools": tools, "warnings": warnings,
            "policy": {"version": catalog.version, "owner": catalog.owner, "reviewed_by": catalog.reviewed_by}}


def render_text(report: dict[str, Any]) -> str:
    out: list[str] = []
    for name, t in report["tools"].items():
        out.append(name)
        if t.get("rate_limit"):
            out.append(f"  at most {t['rate_limit'][0]} calls per {t['rate_limit'][1]:g}s per session")
        for r in t.get("rules", []):
            out.append(f"  must hold        {r}")
        if t.get("always_needs_a_person"):
            out.append("  a person decides every call of this tool")
        for a, src in t["application_fixed"].items():
            out.append(f"  your app fixes   {a}  (from {src}; the model's value is discarded)")
        for a, c in t["model_writable"].items():
            out.append(f"  the model writes {a}  ({', '.join(f'{k} {v}' for k, v in c.items()) or 'no constraints'})")
        for a in t["human_gated"]:
            out.append(f"  a person decides {a}  (every call)")
        for a, s in t["standing_approvals"].items():
            extra = f", at most {s['session_budget']:g} per session" if s["session_budget"] else ", no session total"
            out.append(f"  small ones pass  {a}  ({s['limit']}{extra}); anything else needs a person")
        for a in t["derived"]:
            out.append(f"  computed by formula  {a}")
        out.append("")
    if report["warnings"]:
        out.append("Check these:")
        out += [f"  - {w}" for w in report["warnings"]]
    else:
        out.append("No warnings.")
    return "\n".join(out)
