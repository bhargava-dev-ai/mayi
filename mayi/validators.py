"""
Reusable cross_field_validators helpers, for the common shapes of
"these two blanks only cause trouble together." Each factory returns a
function of the right shape (final_args) -> (bool, reason) to hand to
ToolBoundary(..., cross_field_validators={tool_name: ...}).

These do not make mayi reason about field relationships on its own --
someone still picks which of these to use, and for what. They just mean
not everyone has to write the same five lines of Python from scratch.
"""

from __future__ import annotations

from typing import Any, Callable


def max_combined_unless(
    amount_field: str,
    condition_field: str,
    allowed_values: set,
    max_amount: float,
) -> Callable[[dict], tuple[bool, str]]:
    """
    e.g. max_combined_unless("amount", "currency", {"USD"}, 100):
    amounts over 100 are fine in USD, and refused in anything else.
    """
    def check(final_args: dict) -> tuple[bool, str]:
        amount = final_args.get(amount_field)
        condition = final_args.get(condition_field)
        if condition not in allowed_values and isinstance(amount, (int, float)) and amount > max_amount:
            return False, (
                f"{amount_field}={amount} with {condition_field}={condition!r} exceeds "
                f"{max_amount} (only allowed unconditionally for {sorted(allowed_values)})"
            )
        return True, ""
    return check


def require_together(*fields: str) -> Callable[[dict], tuple[bool, str]]:
    """All of these fields must be present together, or none of them."""
    def check(final_args: dict) -> tuple[bool, str]:
        present = [f for f in fields if final_args.get(f) not in (None, "")]
        if present and len(present) != len(fields):
            missing = [f for f in fields if f not in present]
            return False, f"{', '.join(present)} given without required companion field(s) {', '.join(missing)}"
        return True, ""
    return check


def mutually_exclusive(*fields: str) -> Callable[[dict], tuple[bool, str]]:
    """At most one of these fields may be set."""
    def check(final_args: dict) -> tuple[bool, str]:
        present = [f for f in fields if final_args.get(f) not in (None, "")]
        if len(present) > 1:
            return False, f"fields {', '.join(present)} are mutually exclusive, only one may be set"
        return True, ""
    return check


def combine(*checks: Callable[[dict], tuple[bool, str]]) -> Callable[[dict], tuple[bool, str]]:
    """Run several cross-field checks for one tool; stops at the first failure."""
    def check(final_args: dict) -> tuple[bool, str]:
        for c in checks:
            ok, reason = c(final_args)
            if not ok:
                return ok, reason
        return True, ""
    return check
