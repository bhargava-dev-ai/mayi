"""
Catalog: the machine-readable form of the Gate III artifact.

A catalog maps tool name -> argument name -> ArgSpec, where ArgSpec says
which pen holds that argument and how to resolve or validate it. The
catalog is data, validated once at load time so a misconfigured gate is
caught before any agent runs, not discovered the first time it matters.
"""

from __future__ import annotations

import ast
import operator
import yaml
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .limits import parse_limit


class CatalogError(ValueError):
    """Raised for a malformed or unenforceable catalog. Never silently ignored."""


class Pen(str, Enum):
    APPLICATION = "application"
    MODEL = "model"
    HUMAN = "human"
    DERIVED = "derived"


_SCALAR_TYPES = {"str", "int", "float", "bool"}


@dataclass
class ArgSpec:
    pen: Pen
    source: Optional[str] = None
    max_len: Optional[int] = None
    allowlist: Optional[list] = None
    type: Optional[str] = None
    limit: Optional[str] = None
    session_budget: Optional[float] = None  # cap on the running total of standing approvals per session
    allowlist_from: Optional[str] = None  # dotted path to a list in the session context the value must be in
    forbid: Optional[list] = None  # 'url' and/or 'email': reject model text containing one
    dual_control_above: Optional[float] = None  # human pen: approvals above this need two different people
    untrusted_copy: Optional[str] = None  # 'hold' or 'reject' a model value found verbatim in untrusted text
    fields: Optional[dict[str, "ArgSpec"]] = None  # nested schema, model-pen only in v1

    def __post_init__(self):
        if self.type is not None and self.type == "object" and self.fields is None and self.pen == Pen.MODEL:
            raise CatalogError(
                "a model-pen argument declared as type: object must list its `fields`; "
                "an undeclared structured argument is how every field in it ends up "
                "model-writable whether you meant it or not"
            )
        if self.type is not None and self.type not in _SCALAR_TYPES | {"object"}:
            raise CatalogError(f"unknown type {self.type!r}")


def _build_argspec(arg_name: str, spec: dict) -> ArgSpec:
    spec = spec or {}
    if "pen" not in spec:
        raise CatalogError(f"'{arg_name}': missing required field 'pen'")
    try:
        pen = Pen(spec["pen"])
    except ValueError:
        raise CatalogError(f"'{arg_name}': invalid pen {spec['pen']!r}, must be application/model/human") from None

    fields = None
    if spec.get("fields") is not None:
        if pen != Pen.MODEL:
            raise CatalogError(
                f"'{arg_name}': nested 'fields' are only supported for the model pen in v1 "
                f"(this argument is {pen.value})"
            )
        fields = {k: _build_argspec(f"{arg_name}.{k}", v) for k, v in spec["fields"].items()}
        for sub_name, sub_spec in fields.items():
            if sub_spec.pen != Pen.MODEL:
                raise CatalogError(
                    f"'{arg_name}.{sub_name}': nested fields under a model-pen argument "
                    "must themselves be model-pen in v1"
                )

    if pen == Pen.APPLICATION and spec.get("source") is None:
        raise CatalogError(f"'{arg_name}': application pen requires 'source'")

    if pen == Pen.DERIVED:
        if spec.get("source") is not None:
            raise CatalogError(
                f"'{arg_name}': derived pen computes its value from a human-authored "
                "formula, not a session 'source' -- that belongs to the application pen"
            )
        if spec.get("limit") is not None or spec.get("allowlist") is not None:
            raise CatalogError(
                f"'{arg_name}': derived pen is not approved or allowlisted, it is computed; "
                "'limit' and 'allowlist' belong to the human pen"
            )

    if pen == Pen.HUMAN:
        if spec.get("limit") is not None:
            parse_limit(spec["limit"], arg_name)  # raises CatalogError-compatible ValueError if unenforceable

    budget = spec.get("session_budget")
    if budget is not None:
        if pen != Pen.HUMAN or spec.get("limit") is None:
            raise CatalogError(
                f"'{arg_name}': session_budget caps the running total of standing approvals, "
                "so it needs the human pen and a 'limit'"
            )
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not budget > 0:
            raise CatalogError(f"'{arg_name}': session_budget must be a positive number")

    af = spec.get("allowlist_from")
    if af is not None:
        if pen != Pen.MODEL or not isinstance(af, str) or not af:
            raise CatalogError(f"'{arg_name}': allowlist_from is a dotted path and applies to the model pen only")
        if spec.get("allowlist") is not None:
            raise CatalogError(f"'{arg_name}': use allowlist or allowlist_from, not both")
    fb = spec.get("forbid")
    if fb is not None:
        if pen != Pen.MODEL or not isinstance(fb, list) or not fb or any(x not in ("url", "email") for x in fb):
            raise CatalogError(f"'{arg_name}': forbid is a list of 'url' and/or 'email' and applies to the model pen only")
    dc = spec.get("dual_control_above")
    if dc is not None:
        if pen != Pen.HUMAN or isinstance(dc, bool) or not isinstance(dc, (int, float)):
            raise CatalogError(f"'{arg_name}': dual_control_above is a number and applies to the human pen only")

    uc = spec.get("untrusted_copy")
    if uc is not None:
        if uc not in ("hold", "reject") or pen != Pen.MODEL:
            raise CatalogError(f"'{arg_name}': untrusted_copy must be 'hold' or 'reject' and applies to the model pen only")

    arg_type = spec.get("type")
    if arg_type is not None and arg_type not in _SCALAR_TYPES | {"object"}:
        raise CatalogError(f"'{arg_name}': unknown type {arg_type!r}")

    return ArgSpec(
        pen=pen, source=spec.get("source"), max_len=spec.get("max_len"),
        allowlist=spec.get("allowlist"), type=arg_type, limit=spec.get("limit"),
        session_budget=spec.get("session_budget"), allowlist_from=af, forbid=fb,
        dual_control_above=dc, untrusted_copy=uc, fields=fields,
    )


def _parse_rate(tool: str, raw) -> Optional[tuple]:
    if raw is None:
        return None
    try:
        n, secs = str(raw).split("/")
        n, secs = int(n), float(secs)
        if n < 1 or secs <= 0:
            raise ValueError
    except ValueError:
        raise CatalogError(f"tool '{tool}': _rate_limit must look like \"10/60\" (calls / seconds)") from None
    return (n, secs)


_REL_OPS = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
            ast.Eq: operator.eq, ast.NotEq: operator.ne}


def parse_rule(tool: str, text: str, arg_names: set) -> tuple:
    """'amount <= order_total' or 'amount <= 500' -> (text, left, op, right_kind, right). Nothing else is accepted."""
    try:
        node = ast.parse(str(text), mode="eval").body
    except SyntaxError:
        raise CatalogError(f"tool '{tool}': _require rule {text!r} is not a comparison") from None
    ok = isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _REL_OPS
    if not ok or not isinstance(node.left, ast.Name):
        raise CatalogError(f"tool '{tool}': _require rule {text!r} must be 'argument <op> argument-or-number'")
    right = node.comparators[0]
    if isinstance(right, ast.Name):
        kind, rv = "arg", right.id
    elif isinstance(right, ast.Constant) and isinstance(right.value, (int, float)) and not isinstance(right.value, bool):
        kind, rv = "num", right.value
    else:
        raise CatalogError(f"tool '{tool}': _require rule {text!r} must compare with an argument or a number")
    for nm in (node.left.id, rv if kind == "arg" else node.left.id):
        if nm not in arg_names:
            raise CatalogError(f"tool '{tool}': _require rule {text!r} names {nm!r}, which is not an argument of this tool")
    return (str(text), node.left.id, _REL_OPS[type(node.ops[0])], kind, rv)


@dataclass
class ToolSpec:
    name: str
    args: dict[str, ArgSpec] = field(default_factory=dict)
    rate_limit: Optional[tuple] = None  # (calls, seconds) per session
    require: list = field(default_factory=list)  # relations between arguments, checked on the final values
    requires_approval: bool = False  # the call itself needs a person, whatever its arguments (covers tools with none)


@dataclass
class Catalog:
    tools: dict[str, ToolSpec] = field(default_factory=dict)
    source_hash: Optional[str] = None
    version: Optional[str] = None
    owner: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_on: Optional[str] = None

    def __contains__(self, tool_name: str) -> bool:
        return tool_name in self.tools

    def __getitem__(self, tool_name: str) -> ToolSpec:
        return self.tools[tool_name]

    def check_reachable(self, tools: dict) -> None:
        """
        Raise if anything the runtime could call is missing from the
        catalog. A tool that is reachable and not in the catalog is
        model-writable by default, and that should fail loudly at
        construction time, not be discovered later.
        """
        missing = sorted(set(tools) - set(self.tools))
        if missing:
            raise CatalogError(
                f"tool(s) reachable but not in the catalog, so they have no enforced pens: {missing}"
            )

    @classmethod
    def from_dict(cls, raw: dict) -> "Catalog":
        raw = dict(raw or {})
        meta = raw.pop("_meta", {}) or {}
        if meta and not (meta.get("owner") and meta.get("reviewed_by")):
            raise CatalogError(
                "_meta is present but incomplete: 'owner' and 'reviewed_by' are both "
                "required once you're tracking catalog metadata at all — a half-filled "
                "record is worse than none, because it looks reviewed and isn't"
            )
        tools = {}
        for tool_name, arg_map in raw.items():
            args = {}
            arg_map = dict(arg_map or {})
            needs_person = arg_map.pop("_requires_approval", False)
            if not isinstance(needs_person, bool):
                raise CatalogError(f"tool '{tool_name}': _requires_approval must be true or false")
            rate = _parse_rate(tool_name, arg_map.pop("_rate_limit", None))
            rules = arg_map.pop("_require", None) or []
            if not isinstance(rules, list):
                raise CatalogError(f"tool '{tool_name}': _require must be a list of rules")
            for arg_name, spec in arg_map.items():
                try:
                    args[arg_name] = _build_argspec(arg_name, spec)
                except CatalogError as e:
                    raise CatalogError(f"tool '{tool_name}', argument {e}") from None
            parsed = [parse_rule(tool_name, r, set(args)) for r in rules]
            tools[tool_name] = ToolSpec(name=tool_name, args=args, requires_approval=needs_person,
                                        rate_limit=rate, require=parsed)
        return cls(
            tools=tools, version=meta.get("version"), owner=meta.get("owner"),
            reviewed_by=meta.get("reviewed_by"), reviewed_on=meta.get("reviewed_on"),
        )

    @classmethod
    def from_yaml(cls, path: str) -> "Catalog":
        import hashlib

        with open(path, "rb") as f:
            raw_bytes = f.read()
        catalog = cls.from_dict(yaml.safe_load(raw_bytes))
        catalog.source_hash = hashlib.sha256(raw_bytes).hexdigest()[:16]
        return catalog
