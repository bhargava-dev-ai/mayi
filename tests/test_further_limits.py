import os

from mayi import Catalog, Executed, Rejected, ToolBoundary
from mayi.seal import check_no_direct_exposure, gate
from mayi.validators import combine, max_combined_unless, mutually_exclusive, require_together

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "catalog.yaml")


def resolver(session, tool_name, arg_name):
    if arg_name == "recipient":
        return session["customer_email"]
    if arg_name == "employee_id":
        return session["employee_id"]
    raise KeyError(arg_name)


SESSION = {"customer_email": "real@example.com", "employee_id": "E1"}


def make_boundary(**kwargs):
    return ToolBoundary(
        Catalog.from_yaml(FIXTURE),
        {"send_message": lambda recipient, text: text,
         "recommend": lambda employee_id, amount, rationale: amount},
        resolver, **kwargs,
    )


# ---------------------------------------------------------------- exposure linter

def test_exposure_linter_catches_the_raw_function_handed_out_elsewhere():
    def real_send_message(recipient, text):
        return text
    boundary = ToolBoundary(Catalog.from_yaml(FIXTURE), {"send_message": real_send_message}, resolver)

    # simulate accidentally also registering the raw function with a framework's tool list
    framework_tool_list = [real_send_message]
    warnings = check_no_direct_exposure(boundary, framework_tool_list)

    assert len(warnings) == 1
    assert "send_message" in warnings[0]


def test_exposure_linter_is_clean_when_only_the_gate_is_exposed():
    def real_send_message(recipient, text):
        return text
    boundary = ToolBoundary(Catalog.from_yaml(FIXTURE), {"send_message": real_send_message}, resolver)
    gated = gate(boundary, "send_message", session_provider=lambda: SESSION)

    warnings = check_no_direct_exposure(boundary, [gated])  # the gate itself, not the raw fn

    assert warnings == []


# ---------------------------------------------------------------- validator helper library

def test_max_combined_unless_blocks_non_usd_over_limit():
    check = max_combined_unless("amount", "currency", {"USD"}, 100)
    ok, reason = check({"amount": 200, "currency": "EUR"})
    assert not ok and "100" in reason


def test_max_combined_unless_allows_usd_over_limit():
    check = max_combined_unless("amount", "currency", {"USD"}, 100)
    ok, _ = check({"amount": 200, "currency": "USD"})
    assert ok


def test_require_together_catches_partial_fields():
    check = require_together("street", "city", "zip")
    ok, reason = check({"street": "1 Main St"})
    assert not ok and "city" in reason


def test_require_together_allows_all_or_nothing():
    assert require_together("a", "b")({"a": 1, "b": 2})[0] is True
    assert require_together("a", "b")({})[0] is True


def test_mutually_exclusive_blocks_both_set():
    ok, reason = mutually_exclusive("gift_card", "store_credit")({"gift_card": 1, "store_credit": 1})
    assert not ok


def test_combine_runs_several_checks_stops_at_first_failure():
    check = combine(
        require_together("a", "b"),
        max_combined_unless("amount", "currency", {"USD"}, 100),
    )
    ok, reason = check({"a": 1})  # fails the first check
    assert not ok and "b" in reason


def test_a_validator_helper_wired_into_a_real_boundary_call():
    check = max_combined_unless("amount", "rationale", {"routine"}, 500)
    # reuse the fixture's recommend tool: amount is human-pen there, so
    # this demonstrates wiring a helper into cross_field_validators generally
    boundary = make_boundary(cross_field_validators={"recommend": check})
    result = boundary.call("recommend", {"amount": 9000, "rationale": "surprise"}, SESSION)
    # amount is human-pen and over its own bounded limit, so it escalates
    # before the cross-field check even runs -- this just proves the helper
    # plugs into the same constructor argument as a hand-written function
    from mayi import Escalated
    assert isinstance(result, Escalated)


# ---------------------------------------------------------------- content_validators

def test_content_validator_can_reject_model_text_a_type_check_would_miss():
    def no_dollar_signs(value):
        if "$" in value:
            return False, "message must not quote specific amounts"
        return True, ""

    boundary = make_boundary(content_validators={"send_message": {"text": no_dollar_signs}})
    result = boundary.call("send_message", {"recipient": "x", "text": "that'll be $500"}, SESSION)

    assert isinstance(result, Rejected)
    assert "content check failed" in result.reason


def test_content_validator_allows_text_that_passes():
    def no_dollar_signs(value):
        if "$" in value:
            return False, "message must not quote specific amounts"
        return True, ""

    boundary = make_boundary(content_validators={"send_message": {"text": no_dollar_signs}})
    result = boundary.call("send_message", {"recipient": "x", "text": "all set, thanks!"}, SESSION)

    assert isinstance(result, Executed)


def test_no_content_validator_registered_means_no_such_check():
    boundary = make_boundary()  # no content_validators at all
    result = boundary.call("send_message", {"recipient": "x", "text": "that'll be $500"}, SESSION)
    assert isinstance(result, Executed)  # nothing was registered to catch it
