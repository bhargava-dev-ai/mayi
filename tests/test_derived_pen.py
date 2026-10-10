"""
The derived pen: a value computed by a human-authored formula from the
other already-resolved arguments on the same call -- not supplied by the
model, and not read from the session either. Example: an INR amount
computed from a USD amount and a trusted exchange rate.
"""

import pytest

from mayi import Catalog, CatalogError, Executed, Rejected, ToolBoundary


def resolver(session, tool_name, arg_name):
    if arg_name == "rate":
        return session["rate"]
    raise KeyError(arg_name)


def make_catalog():
    return Catalog.from_dict({
        "send_payment": {
            "amount_usd": {"pen": "model", "type": "float"},
            "rate": {"pen": "application", "source": "rate"},
            "amount_inr": {"pen": "derived"},
        }
    })


def convert(final):
    return round(final["amount_usd"] * final["rate"], 2)


def test_derived_value_is_computed_from_other_resolved_args():
    boundary = ToolBoundary(
        make_catalog(),
        {"send_payment": lambda amount_usd, rate, amount_inr: amount_inr},
        resolver,
        derivations={"send_payment": {"amount_inr": convert}},
    )
    session = {"rate": 83.5}

    result = boundary.call("send_payment", {"amount_usd": 100.0, "amount_inr": 1.0}, session)

    assert isinstance(result, Executed)
    assert result.args["amount_inr"] == 8350.0


def test_derived_pen_ignores_whatever_the_model_proposed():
    # The model's own guess at amount_inr (1.0, wildly wrong) must never
    # reach the tool -- same discard-on-sight treatment as application pen.
    boundary = ToolBoundary(
        make_catalog(),
        {"send_payment": lambda amount_usd, rate, amount_inr: amount_inr},
        resolver,
        derivations={"send_payment": {"amount_inr": convert}},
    )
    session = {"rate": 83.5}

    result = boundary.call("send_payment", {"amount_usd": 100.0, "amount_inr": 999999.0}, session)

    assert isinstance(result, Executed)
    assert result.args["amount_inr"] == 8350.0


def test_missing_derivation_function_fails_at_construction_not_call_time():
    with pytest.raises(CatalogError, match="no derivation function was registered"):
        ToolBoundary(
            make_catalog(),
            {"send_payment": lambda amount_usd, rate, amount_inr: amount_inr},
            resolver,
            derivations={},  # nothing registered for amount_inr
        )


def test_derivation_raising_rejects_the_call_instead_of_crashing():
    def broken(final):
        raise ZeroDivisionError("rate was zero")

    boundary = ToolBoundary(
        make_catalog(),
        {"send_payment": lambda amount_usd, rate, amount_inr: amount_inr},
        resolver,
        derivations={"send_payment": {"amount_inr": broken}},
    )
    session = {"rate": 0}

    result = boundary.call("send_payment", {"amount_usd": 100.0, "amount_inr": 1.0}, session)

    assert isinstance(result, Rejected)
    assert "derivation failed" in result.reason


def test_derived_pen_rejects_source_in_catalog():
    with pytest.raises(CatalogError, match="not a session 'source'"):
        Catalog.from_dict({
            "send_payment": {"amount_inr": {"pen": "derived", "source": "foo"}}
        })


def test_derived_pen_rejects_limit_and_allowlist_in_catalog():
    with pytest.raises(CatalogError, match="not approved or allowlisted"):
        Catalog.from_dict({
            "send_payment": {"amount_inr": {"pen": "derived", "limit": "amount_inr <= 500"}}
        })


def test_later_derived_arg_can_read_an_earlier_derived_arg():
    catalog = Catalog.from_dict({
        "send_payment": {
            "amount_usd": {"pen": "model", "type": "float"},
            "rate": {"pen": "application", "source": "rate"},
            "amount_inr": {"pen": "derived"},
            "amount_inr_with_fee": {"pen": "derived"},
        }
    })
    boundary = ToolBoundary(
        catalog,
        {"send_payment": lambda **kw: kw["amount_inr_with_fee"]},
        resolver,
        derivations={
            "send_payment": {
                "amount_inr": convert,
                "amount_inr_with_fee": lambda final: round(final["amount_inr"] * 1.02, 2),
            }
        },
    )
    session = {"rate": 83.5}

    result = boundary.call(
        "send_payment",
        {"amount_usd": 100.0, "amount_inr": 0, "amount_inr_with_fee": 0},
        session,
    )

    assert isinstance(result, Executed)
    assert result.args["amount_inr"] == 8350.0
    assert result.args["amount_inr_with_fee"] == 8517.0
