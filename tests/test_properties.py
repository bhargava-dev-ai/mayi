"""
Hand-written tests check specific scenarios. These check invariants that
must hold for EVERY input, by throwing thousands of randomly generated
adversarial values at the boundary and asserting the guarantee never
breaks, no matter what the model proposes.
"""

import os

from hypothesis import given, settings, strategies as st

from mayi import ApprovalSet, Catalog, Executed, Rejected, ToolBoundary

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "catalog.yaml")
REAL_CUSTOMER = "real.customer@example.com"


def resolver(session, tool_name, arg_name):
    if arg_name == "recipient":
        return session["customer_email"]
    if arg_name == "employee_id":
        return session["employee_id"]
    raise KeyError(arg_name)


def make_boundary(sent):
    tools = {
        "send_message": lambda recipient, text: sent.append(recipient) or "sent",
        "recommend": lambda employee_id, amount, rationale: (employee_id, amount),
    }
    return ToolBoundary(Catalog.from_yaml(FIXTURE), tools, resolver)


SESSION = {"customer_email": REAL_CUSTOMER, "employee_id": "E1"}

# Any string at all, including ones that look like injected instructions,
# other emails, empty strings, or control characters.
any_proposed_recipient = st.one_of(
    st.emails(),
    st.text(min_size=0, max_size=500),
    st.just(REAL_CUSTOMER),  # even proposing the "right" value must not matter
)


@settings(max_examples=300)
@given(proposed_recipient=any_proposed_recipient, text=st.text(max_size=2000))
def test_no_matter_what_the_model_proposes_recipient_is_always_the_real_one(proposed_recipient, text):
    sent = []
    boundary = make_boundary(sent)

    result = boundary.call("send_message", {"recipient": proposed_recipient, "text": text}, SESSION)

    assert isinstance(result, Executed)
    assert sent == [REAL_CUSTOMER]


@settings(max_examples=300)
@given(amount=st.integers(min_value=-10_000, max_value=10_000))
def test_amount_within_limit_always_executes_and_never_needs_a_token(amount):
    sent = []
    boundary = make_boundary(sent)

    result = boundary.call("recommend", {"amount": amount, "rationale": "r"}, SESSION)

    if amount <= 500:
        assert isinstance(result, Executed)
    else:
        assert not isinstance(result, Executed)  # Escalated, never silently allowed


@settings(max_examples=200)
@given(text=st.text(min_size=2001, max_size=4000))
def test_oversized_model_text_is_always_rejected_never_executed(text):
    sent = []
    boundary = make_boundary(sent)

    result = boundary.call("send_message", {"recipient": "x", "text": text}, SESSION)

    assert isinstance(result, Rejected)
    assert sent == []


@settings(max_examples=200)
@given(extra_key=st.text(min_size=1, max_size=20).filter(lambda s: s not in {"recipient", "text"}))
def test_any_unknown_argument_name_is_always_rejected(extra_key):
    sent = []
    boundary = make_boundary(sent)

    result = boundary.call(
        "send_message", {"recipient": "x", "text": "hi", extra_key: "attacker@evil.com"}, SESSION,
    )

    assert isinstance(result, Rejected)
    assert sent == []


@settings(max_examples=200)
@given(nested=st.dictionaries(st.text(max_size=10), st.text(max_size=10), max_size=5))
def test_arbitrary_dict_proposed_for_a_scalar_model_arg_is_always_rejected(nested):
    sent = []
    boundary = make_boundary(sent)

    result = boundary.call("send_message", {"recipient": "x", "text": nested}, SESSION)

    assert isinstance(result, Rejected)
    assert sent == []


@settings(max_examples=200)
@given(amount=st.integers(min_value=0, max_value=500), currency=st.sampled_from(["USD", "EUR", "GBP"]))
def test_cross_field_validator_is_deterministic_across_many_combinations(amount, currency):
    from mayi import Catalog

    def check(final_args):
        if final_args.get("currency") != "USD" and final_args.get("amount", 0) > 100:
            return False, "non-USD over 100"
        return True, ""

    catalog = Catalog.from_dict({"pay": {
        "amount": {"pen": "human", "limit": "amount <= 500"},
        "currency": {"pen": "model", "type": "str", "allowlist": ["USD", "EUR", "GBP"]},
    }})
    boundary = ToolBoundary(
        catalog, {"pay": lambda amount, currency: None},
        session_resolver=lambda s, t, a: None, cross_field_validators={"pay": check},
    )

    result = boundary.call("pay", {"amount": amount, "currency": currency}, {})

    should_pass = currency == "USD" or amount <= 100
    assert isinstance(result, Executed) == should_pass
