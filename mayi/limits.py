"""
Parses a catalog `limit` expression, e.g. "amount <= 500", into a safe
callable. This is NOT a general expression evaluator: it accepts exactly
one comparison of the argument's own name against a numeric literal.
Anything else is rejected at catalog-load time, on purpose, so a limit
can never become a place to smuggle in logic nobody reviewed.
"""

from __future__ import annotations

import ast
import operator

_OPS = {
    ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
}


def parse_limit(expr: str, arg_name: str):
    try:
        node = ast.parse(expr, mode="eval").body
    except SyntaxError as e:
        raise ValueError(f"invalid limit expression {expr!r}: {e}") from e

    if not (isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1):
        raise ValueError(f"limit must be a single comparison, got {expr!r}")

    left, op, right = node.left, node.ops[0], node.comparators[0]
    if not (isinstance(left, ast.Name) and left.id == arg_name):
        raise ValueError(f"limit must compare the argument '{arg_name}', got {expr!r}")
    if not (isinstance(right, ast.Constant) and isinstance(right.value, (int, float)) and not isinstance(right.value, bool)):
        raise ValueError(f"limit must compare against a number, got {expr!r}")
    if type(op) not in _OPS:
        raise ValueError(f"unsupported operator in limit {expr!r}")

    fn, bound = _OPS[type(op)], right.value

    def check(value) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and fn(value, bound)

    return check
