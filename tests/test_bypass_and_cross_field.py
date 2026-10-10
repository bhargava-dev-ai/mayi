"""
Two things this file does NOT claim to solve, but does make concrete:

1. Tool bypass (review's Challenge B / Stage 3). mayi cannot stop code
   that holds a direct reference to the real tool function from calling
   it directly -- no Python library can prevent that from inside the
   same process. What this test does is make the risk executable rather
   than theoretical: it shows the injection succeeding when the raw
   function is called directly, and failing when the exact same call is
   routed through the boundary. That is the proof that "don't hand out
   the raw callable" is not a suggestion, it is the whole ballgame.

2. Cross-field dependencies (review's Challenge C / Stage 4). mayi now
   supports an OPTIONAL per-tool validator that runs after every
   individual argument has been resolved, so a tool author can reject a
   combination of values even when each one was individually fine. This
   does not make mayi reason about field relationships on its own --
   the validator is still hand-written by a human -- but it gives that
   human a place to put the logic instead of nowhere at all.
"""

import os

from mayi import Catalog, Executed, Rejected, ToolBoundary

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "catalog.yaml")


def resolver(session, tool_name, arg_name):
    if arg_name == "recipient":
        return session["customer_email"]
    raise KeyError(arg_name)


# ---------------------------------------------------------------- tool bypass, made concrete

def test_bypassing_the_boundary_lets_the_injection_through():
    """
    This is the vulnerability, proven rather than asserted: the exact
    same malicious call that mayi defeats below succeeds instantly when
    someone (an agent framework, a careless integration, a future
    refactor) holds a direct reference to the real function.
    """
    sent = []

    def real_send_message(recipient, text):
        sent.append(recipient)

    malicious_args = {"recipient": "attacker@evil.com", "text": "hi"}

    # THE BYPASS: calling the real function directly, boundary skipped entirely.
    real_send_message(**malicious_args)
    assert sent == ["attacker@evil.com"]  # <-- the vulnerability, executed


def test_the_same_call_through_the_boundary_is_defeated():
    sent = []

    def real_send_message(recipient, text):
        sent.append(recipient)

    boundary = ToolBoundary(Catalog.from_yaml(FIXTURE), {"send_message": real_send_message}, resolver)
    malicious_args = {"recipient": "attacker@evil.com", "text": "hi"}

    boundary.call("send_message", malicious_args, {"customer_email": "real@example.com"})
    assert sent == ["real@example.com"]  # <-- the fix, but ONLY because the call went through the boundary


# ---------------------------------------------------------------- optional cross-field validator

def test_cross_field_validator_can_reject_a_combination_individual_checks_miss():
    # Individually: amount is within the 500 limit, currency is a valid string.
    # Together: this tool never allows non-USD amounts over 100. Neither pen
    # alone can express that; a cross-field validator can.
    def currency_amount_check(final_args: dict):
        if final_args.get("currency") != "USD" and final_args.get("amount", 0) > 100:
            return False, "non-USD amounts over 100 require a currency-specific review"
        return True, ""

    catalog = Catalog.from_dict({
        "pay": {
            "amount": {"pen": "human", "limit": "amount <= 500"},
            "currency": {"pen": "model", "type": "str", "allowlist": ["USD", "EUR", "GBP"]},
        }
    })
    boundary = ToolBoundary(
        catalog, {"pay": lambda amount, currency: (amount, currency)},
        session_resolver=lambda s, t, a: None,
        cross_field_validators={"pay": currency_amount_check},
    )

    result = boundary.call("pay", {"amount": 200, "currency": "EUR"}, {})
    assert isinstance(result, Rejected)
    assert "non-USD amounts over 100" in result.reason


def test_cross_field_validator_allows_a_combination_that_passes():
    def currency_amount_check(final_args: dict):
        if final_args.get("currency") != "USD" and final_args.get("amount", 0) > 100:
            return False, "non-USD amounts over 100 require a currency-specific review"
        return True, ""

    catalog = Catalog.from_dict({
        "pay": {
            "amount": {"pen": "human", "limit": "amount <= 500"},
            "currency": {"pen": "model", "type": "str", "allowlist": ["USD", "EUR", "GBP"]},
        }
    })
    boundary = ToolBoundary(
        catalog, {"pay": lambda amount, currency: (amount, currency)},
        session_resolver=lambda s, t, a: None,
        cross_field_validators={"pay": currency_amount_check},
    )

    result = boundary.call("pay", {"amount": 50, "currency": "EUR"}, {})
    assert isinstance(result, Executed)


def test_no_validator_registered_means_no_cross_field_check_at_all():
    # Honest default: without a validator, mayi does not invent one.
    # This is Challenge C, left exactly as open as it actually is when unused.
    catalog = Catalog.from_dict({
        "pay": {
            "amount": {"pen": "human", "limit": "amount <= 500"},
            "currency": {"pen": "model", "type": "str", "allowlist": ["USD", "EUR", "GBP"]},
        }
    })
    boundary = ToolBoundary(
        catalog, {"pay": lambda amount, currency: (amount, currency)},
        session_resolver=lambda s, t, a: None,
    )
    result = boundary.call("pay", {"amount": 200, "currency": "EUR"}, {})
    assert isinstance(result, Executed)  # nothing caught the combination; nothing was registered to
