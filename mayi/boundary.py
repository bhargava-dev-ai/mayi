"""
The tool boundary: the code that sits between the model's proposed tool
call and the real tool. This is the only thing that executes tools.

Nothing about "be careful" lives in a prompt here. Every argument is
resolved, validated, or gated according to the catalog, and the model
cannot see or influence application-pen values, cannot bypass a
human-pen approval or standing limit, and cannot smuggle an argument
(or a nested field) the catalog doesn't know about.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from .approvals import ApprovalSet
from .catalog import ArgSpec, Catalog, CatalogError, Pen
from .limits import parse_limit
from .provenance import copied_from
from .resolve import dotted_lookup
from .results import Escalated, Executed, Rejected


class ToolBoundary:
    def __init__(
        self,
        catalog: Catalog,
        tools: dict[str, Callable[..., Any]],
        session_resolver: Optional[Callable[[Any, str, str], Any]] = None,
        audit_log: Optional[Callable[[dict], None]] = None,
        strict: bool = True,
        cross_field_validators: Optional[dict[str, Callable[[dict], tuple[bool, str]]]] = None,
        content_validators: Optional[dict[str, dict[str, Callable[[Any], tuple[bool, str]]]]] = None,
        derivations: Optional[dict[str, dict[str, Callable[[dict], Any]]]] = None,
        transforms: Optional[dict[str, str]] = None,
        state: Optional[Any] = None,
    ):
        """
        catalog: the loaded Gate III artifact.
        tools: the real callables, keyed by tool name. Never exposed
            directly to the model.
        session_resolver: (session, tool_name, arg_name) -> value.
            Resolves an application-pen argument from session context.
            The model's proposed value for this argument is never
            consulted. Optional: if omitted, the argument's `source`
            (e.g. "ticket.customer_email") is looked up on the session
            by dotted path automatically (dict keys, then attributes,
            at each step). Pass your own resolver for anything the
            dotted path can't express.
        audit_log: called with a structured record on every attempt,
            whatever the outcome.
        strict: if True (default), raise at construction time when a
            tool in `tools` is missing from the catalog, instead of
            waiting to discover an unenforced tool at call time.
        cross_field_validators: optional {tool_name: fn(final_args) ->
            (ok, reason)}, run after every individual argument has been
            resolved/validated/approved, before execution. mayi does not
            reason about relationships between arguments on its own --
            this is a place for a human to write that logic, not a
            feature that infers it. Unregistered tools get no check.
        content_validators: optional {tool_name: {arg_name: fn(value) ->
            (ok, reason)}}, run on a model-pen argument's value after
            the built-in type/length/allowlist checks pass. mayi cannot
            judge whether the AI's free text is wise or accurate on its
            own -- this is a place to plug in something that can (a
            moderation call, a denylist, a classifier), not a claim
            that mayi does this itself. No validator registered means
            no such check, same as today.
        derivations: optional {tool_name: {arg_name: fn(final_so_far) ->
            value}}, required for every argument the catalog declares as
            Pen.DERIVED. The model's proposed value for that argument is
            discarded, same as the application pen -- but instead of
            being read from the session, it is computed by this
            human-authored formula from the other arguments already
            resolved for this call (application, human, and model pens
            always run first; a derived argument can read an earlier
            derived argument's value too, in catalog declaration order).
            This exists for values that neither the model nor the raw
            session owns outright -- an amount converted at a trusted
            exchange rate, a total computed from line items already on
            the form -- where the honest answer is "neither of the three
            pens wrote this, a fixed formula did." mayi does not infer
            the formula; a person writes it, same as a cross_field_validator.
        transforms: optional {tool_name: destination_tool_name}, a fixed,
            human-authored redirect table decided in advance -- never
            computed from intent, risk, or anything the model wrote.
            When the model calls `tool_name`, its arguments are still
            resolved and gated entirely under `tool_name`'s own catalog
            entry (the pens, the approvals, the validators -- nothing
            about authority changes), but the already-finalized
            arguments are executed against `destination_tool_name`
            instead. The destination must be a key in `tools`, and in
            strict mode (the default) that means it must also have its
            own catalog entry like any other reachable tool -- a
            transform target is not a side door that skips being
            catalogued, it is itself a real tool a model could also call
            directly under its own name. Use this for the narrow case of "this
            exact call is never allowed to run as proposed, but a
            specific, pre-approved different action should run instead"
            (delete_user -> request_user_deletion) -- not as a general
            rewrite engine, and never with a destination chosen at
            runtime.
        """
        self.catalog = catalog
        self.tools = tools
        self.session_resolver = session_resolver
        self.audit_log = audit_log or (lambda record: None)
        self.cross_field_validators = cross_field_validators or {}
        self.content_validators = content_validators or {}
        self.derivations = derivations or {}
        self.transforms = transforms or {}
        self._state = state  # optional StateStore: makes budget totals survive a restart
        self._calls: dict[tuple, deque] = {}  # (budget_key, tool) -> times of recent executions, for _rate_limit
        self._spent: dict[tuple, float] = {}  # (budget_key, tool, arg) -> running total of executed values
        self._lock = threading.RLock()
        self._check_derivations_registered()
        self._check_transforms_registered()
        if strict:
            catalog.check_reachable(tools)

    def spent(self, budget_key: Any, tool_name: str, arg_name: str) -> float:
        """Running total of executed values counted against this argument's session_budget."""
        if self._state is not None:
            return self._state.spent((budget_key, tool_name, arg_name))
        return self._spent.get((budget_key, tool_name, arg_name), 0.0)

    def reset_budget(self, budget_key: Any) -> None:
        with self._lock:
            if self._state is not None:
                self._state.clear_spent(budget_key)
            for k in [k for k in self._spent if k[0] == budget_key]:
                del self._spent[k]

    def simulate(self, tool_name, model_args, session, approvals=None, budget_key=None, untrusted=None):
        """
        Dry run: what would happen to this call under the current catalog,
        without executing anything, consuming an approval, or writing to the
        audit log. Returns the same Executed/Rejected/Escalated outcome call()
        would (Executed has simulated=True and result None). Derived formulas
        and validators do run, so they should be side-effect free.
        """
        return self.call(tool_name, model_args, session, approvals=approvals, dry_run=True, budget_key=budget_key, untrusted=untrusted)

    def _check_transforms_registered(self) -> None:
        """
        A transform whose destination isn't a real, catalogued tool is a
        typo waiting to raise at call time instead of at construction
        time -- and more importantly, it would be a redirect to
        somewhere nothing governs.
        """
        for tool_name, destination in self.transforms.items():
            if destination not in self.tools:
                raise CatalogError(
                    f"transform '{tool_name}' -> '{destination}': destination is not a "
                    "registered tool, so nothing would govern what actually runs"
                )

    def _check_derivations_registered(self) -> None:
        """
        A DERIVED argument with no matching formula is a policy that
        looks enforced and isn't -- fail at construction time, not the
        first time that tool is called.
        """
        for tool_name, spec in self.catalog.tools.items():
            for arg_name, arg_spec in spec.args.items():
                if arg_spec.pen == Pen.DERIVED:
                    fn = self.derivations.get(tool_name, {}).get(arg_name)
                    if fn is None:
                        raise CatalogError(
                            f"tool '{tool_name}', argument '{arg_name}': declared as the "
                            "derived pen but no derivation function was registered for it"
                        )

    def call(
        self,
        tool_name: str,
        model_args: dict[str, Any],
        session: Any,
        approvals: Optional[ApprovalSet] = None,
        actor: Optional[str] = None,
        dry_run: bool = False,
        budget_key: Any = None,
        untrusted: Optional[list] = None,
    ) -> Executed | Rejected | Escalated:
        """
        budget_key: identifies the session a `session_budget` accumulates over (the gateway
        passes the session id). If omitted, the session's own "session_id" is used when it has
        one; otherwise there is no stable identity to total over, so a standing approval on a
        budgeted argument is not granted and a human decides every time (fails closed).
        """
        spec = self.catalog[tool_name] if tool_name in self.catalog else None
        if budget_key is None:
            budget_key = session.get("session_id") if isinstance(session, dict) else getattr(session, "session_id", None)
        budgeted = spec is not None and any(a.session_budget for a in spec.args.values())
        if budgeted:
            with self._lock:  # check-and-commit must not interleave with another call on the same budget
                return self._call(tool_name, model_args, session, approvals, actor, dry_run, budget_key, untrusted)
        return self._call(tool_name, model_args, session, approvals, actor, dry_run, budget_key, untrusted)

    def _call(self, tool_name, model_args, session, approvals, actor, dry_run, budget_key, untrusted=None):
        approvals = approvals or ApprovalSet()
        audit = (lambda outcome, *a, **k: outcome) if dry_run else self._audit_and_return

        if tool_name not in self.catalog:
            return audit(
                Rejected(tool_name, "unknown tool", {"tool": tool_name}),
                tool_name, model_args, actor,
            )

        spec = self.catalog[tool_name]
        unknown = set(model_args) - set(spec.args)
        if unknown:
            return audit(
                Rejected(tool_name, "unknown argument(s)", {"unknown": sorted(unknown)}),
                tool_name, model_args, actor,
            )

        if spec.rate_limit:
            n_max, window = spec.rate_limit
            dq = self._calls.setdefault((budget_key if budget_key is not None else "__none__", tool_name), deque())
            now_m = time.monotonic()
            while dq and now_m - dq[0] > window:
                dq.popleft()
            if len(dq) >= n_max:
                return audit(Rejected(tool_name, f"rate limit: {n_max} calls per {window:g}s for this session"),
                             tool_name, model_args, actor)

        if spec.requires_approval:
            lookup = approvals.peek if dry_run else approvals.take
            if lookup(tool_name, "_call", True, context={}) is None:
                return audit(Escalated(tool_name, "_call", True, reason="missing human approval (this tool always needs a person)"),
                             tool_name, model_args, actor)

        final: dict[str, Any] = {}
        pens_used: dict[str, str] = {}
        notes: dict[str, str] = {}
        derived_args: list[tuple[str, ArgSpec]] = []
        app_values: dict[str, Any] = {
            n: self._resolve_application(a, session, tool_name, n)
            for n, a in spec.args.items() if a.pen == Pen.APPLICATION
        }

        for arg_name, arg_spec in spec.args.items():
            pens_used[arg_name] = arg_spec.pen.value

            if arg_spec.pen == Pen.DERIVED:
                # Computed after every other pen has resolved, from the
                # finalized arguments -- never from what the model proposed.
                derived_args.append((arg_name, arg_spec))
                continue

            if arg_spec.pen == Pen.APPLICATION:
                final[arg_name] = app_values[arg_name]

            elif arg_spec.pen == Pen.HUMAN:
                proposed = model_args.get(arg_name)

                budget_note = None
                if arg_spec.limit and parse_limit(arg_spec.limit, arg_name)(proposed):
                    cap = arg_spec.session_budget
                    if not cap:
                        final[arg_name] = proposed
                        notes[arg_name] = f"standing approval: within {arg_spec.limit}"
                        continue
                    spent = self.spent(budget_key, tool_name, arg_name)
                    if budget_key is None:
                        budget_note = "no session identity to total over"
                    elif proposed < 0:
                        budget_note = "negative values never count as standing approval"
                    elif spent + proposed <= cap:
                        final[arg_name] = proposed
                        notes[arg_name] = (f"standing approval: within {arg_spec.limit}; "
                                           f"session total {spent + proposed:g} of {cap:g}")
                        continue
                    else:
                        budget_note = f"session budget used up ({spent:g} of {cap:g} spent)"

                token, dual_note = self._approval(approvals, dry_run, arg_spec, tool_name, arg_name, proposed, app_values)
                if token is None:
                    return audit(
                        Escalated(tool_name, arg_name, proposed,
                                  reason="missing human approval" + (f" ({budget_note})" if budget_note else "")
                                  + (f" ({dual_note})" if dual_note else "")),
                        tool_name, model_args, actor, pens_used,
                    )
                final[arg_name] = proposed
                notes[arg_name] = f"approved by {token.approved_by}"

            else:  # MODEL pen
                value = model_args.get(arg_name)
                ok, reason = self._validate_model_value(value, arg_spec, arg_name)
                if not ok:
                    return audit(
                        Rejected(tool_name, f"invalid model argument: {reason}"),
                        tool_name, model_args, actor, pens_used,
                    )
                if isinstance(value, str) and arg_spec.forbid:
                    hit = [k for k in arg_spec.forbid if _FORBID[k].search(value)]
                    if hit:
                        return audit(
                            Rejected(tool_name, f"'{arg_name}' contains a {' / '.join(hit)}, which this argument forbids"),
                            tool_name, model_args, actor, pens_used,
                        )
                if arg_spec.allowlist_from:
                    try:
                        allowed = dotted_lookup(session, arg_spec.allowlist_from)
                    except Exception:  # noqa: BLE001 -- a missing list means nothing is allowed
                        allowed = None
                    if not isinstance(allowed, (list, tuple, set)) or value not in allowed:
                        return audit(
                            Rejected(tool_name, f"'{arg_name}' is not in the list this session allows ({arg_spec.allowlist_from})"),
                            tool_name, model_args, actor, pens_used,
                        )
                content_check = self.content_validators.get(tool_name, {}).get(arg_name)
                if content_check is not None:
                    ok, reason = content_check(value)
                    if not ok:
                        return audit(
                            Rejected(tool_name, f"content check failed for '{arg_name}': {reason}"),
                            tool_name, model_args, actor, pens_used,
                        )
                if arg_spec.untrusted_copy and untrusted and copied_from(value, untrusted):
                    if arg_spec.untrusted_copy == "reject":
                        return audit(
                            Rejected(tool_name, f"'{arg_name}' was copied verbatim from untrusted content"),
                            tool_name, model_args, actor, pens_used,
                        )
                    lookup = approvals.peek if dry_run else approvals.take
                    token = lookup(tool_name, arg_name, value, context=app_values)
                    if token is None:
                        return audit(
                            Escalated(tool_name, arg_name, value,
                                      reason="missing human approval (value copied verbatim from untrusted content)"),
                            tool_name, model_args, actor, pens_used,
                        )
                    notes[arg_name] = f"copied from untrusted content; approved by {token.approved_by}"
                final[arg_name] = value

        for arg_name, arg_spec in derived_args:
            fn = self.derivations[tool_name][arg_name]
            try:
                final[arg_name] = fn(dict(final))
            except Exception as e:  # noqa: BLE001 -- a bad formula must reject, not crash the boundary
                return audit(
                    Rejected(tool_name, f"derivation failed for '{arg_name}': {e}"),
                    tool_name, model_args, actor, pens_used, notes,
                )
            notes[arg_name] = "derived"

        for text, left, op, kind, right in spec.require:
            lv, rv = final.get(left), (final.get(right) if kind == "arg" else right)
            ok = _comparable(lv, rv)
            if not (ok and op(lv, rv)):
                return audit(Rejected(tool_name, f"rule not met: {text}"), tool_name, model_args, actor, pens_used, notes)

        validator = self.cross_field_validators.get(tool_name)
        if validator is not None:
            ok, reason = validator(final)
            if not ok:
                return audit(
                    Rejected(tool_name, f"cross-field check failed: {reason}"),
                    tool_name, model_args, actor, pens_used, notes,
                )

        executed_tool = self.transforms.get(tool_name, tool_name)
        reserved: list[tuple] = []
        if not dry_run and spec.rate_limit:
            self._calls.setdefault((budget_key if budget_key is not None else "__none__", tool_name), deque()).append(time.monotonic())
        if not dry_run:  # reserve budget BEFORE running: a crash mid-call then counts as spent, not as free
            for name, a in spec_args_with_budget(self.catalog[tool_name]):
                v = final.get(name)
                if budget_key is not None and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                    k = (budget_key, tool_name, name)
                    if self._state is not None:
                        self._state.add_spent(k, v)
                    else:
                        self._spent[k] = self._spent.get(k, 0.0) + v
                    reserved.append((k, v))
        try:
            result = None if dry_run else self.tools[executed_tool](**final)
        except BaseException:
            for k, v in reserved:  # the tool raised, so nothing was spent
                if self._state is not None:
                    self._state.add_spent(k, -v)
                else:
                    self._spent[k] = self._spent.get(k, 0.0) - v
            raise
        executed_as = executed_tool if executed_tool != tool_name else None
        if executed_as:
            notes["__transform__"] = f"redirected to '{executed_as}'"
        return audit(
            Executed(tool_name, final, result, executed_as=executed_as, simulated=dry_run),
            tool_name, model_args, actor, pens_used, notes,
        )

    @staticmethod
    def _approval(approvals, dry_run, spec, tool, arg, value, ctx):
        """(token, note). A value over `dual_control_above` needs a token carrying two different approvers."""
        tok = approvals.peek(tool, arg, value, context=ctx)
        if tok is None:
            return None, None
        t = spec.dual_control_above
        if t is not None and isinstance(value, (int, float)) and not isinstance(value, bool) and value > t:
            if len(set(tok.approvers or (tok.approved_by,))) < 2:
                return None, f"over {t:g}: needs two different approvers"
        return (tok if dry_run else approvals.take(tool, arg, value, context=ctx)), None

    def _resolve_application(self, spec: ArgSpec, session: Any, tool_name: str, path: str) -> Any:
        if self.session_resolver is not None:
            return self.session_resolver(session, tool_name, path)
        return dotted_lookup(session, spec.source)

    def _validate_model_value(self, value: Any, spec: ArgSpec, path: str) -> tuple[bool, str]:
        if spec.fields is not None:
            if not isinstance(value, dict):
                return False, f"'{path}' expected an object with fields {sorted(spec.fields)}"
            unknown = set(value) - set(spec.fields)
            if unknown:
                return False, f"'{path}' has undeclared field(s) {sorted(unknown)}"
            for key, sub_spec in spec.fields.items():
                ok, reason = self._validate_model_value(value.get(key), sub_spec, f"{path}.{key}")
                if not ok:
                    return False, reason
            return True, ""

        if isinstance(value, (dict, list)):
            return False, f"'{path}' is a structured value with no declared fields, refusing to guess its shape"

        if spec.type == "str" and not isinstance(value, str):
            return False, f"'{path}' expected str"
        if spec.type == "int" and not isinstance(value, int):
            return False, f"'{path}' expected int"
        if spec.max_len is not None and isinstance(value, str) and len(value) > spec.max_len:
            return False, f"'{path}' exceeds max_len {spec.max_len}"
        if spec.allowlist is not None and value not in spec.allowlist:
            return False, f"'{path}' not in allowlist {spec.allowlist}"
        return True, ""

    def _audit_and_return(self, outcome, tool_name, model_args, actor, pens_used=None, notes=None):
        detail = {}
        if isinstance(outcome, Rejected):
            detail["reason"] = outcome.reason
        elif isinstance(outcome, Escalated):
            detail["escalated_arg"] = outcome.arg
            detail["escalated_value"] = outcome.proposed_value
        elif isinstance(outcome, Executed) and outcome.executed_as:
            detail["executed_as"] = outcome.executed_as

        self.audit_log({
            "tool": tool_name,
            "outcome": type(outcome).__name__,
            "model_proposed": model_args,
            "pens": pens_used or {},
            "notes": notes or {},
            "actor": actor,
            "catalog_hash": self.catalog.source_hash,
            "catalog_version": self.catalog.version,
            "catalog_reviewed_by": self.catalog.reviewed_by,
            **detail,
        })
        return outcome


_FORBID = {"url": re.compile(r"(https?://|www\.)\S+", re.I), "email": re.compile(r"[\w.+\-]+@[\w\-]+(\.[\w\-]+)+")}


def _comparable(a, b) -> bool:
    num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)  # noqa: E731
    return (num(a) and num(b)) or (isinstance(a, str) and isinstance(b, str))


def spec_args_with_budget(tool_spec):
    return [(n, a) for n, a in tool_spec.args.items() if a.session_budget]
