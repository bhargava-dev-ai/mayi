import os

import pytest

from mayi import Catalog, Executed, Rejected, SourcePathError, ToolBoundary, dotted_lookup

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "catalog.yaml")


class Ticket:
    def __init__(self, customer_email):
        self.customer_email = customer_email


def test_dotted_lookup_over_nested_dicts():
    assert dotted_lookup({"a": {"b": {"c": 42}}}, "a.b.c") == 42


def test_dotted_lookup_over_nested_objects():
    session = type("S", (), {"ticket": Ticket("x@example.com")})()
    assert dotted_lookup(session, "ticket.customer_email") == "x@example.com"


def test_dotted_lookup_mixes_dicts_and_objects():
    session = {"ticket": Ticket("mixed@example.com")}
    assert dotted_lookup(session, "ticket.customer_email") == "mixed@example.com"


def test_dotted_lookup_raises_a_clear_error_on_a_missing_step():
    with pytest.raises(SourcePathError, match="'missing' not found"):
        dotted_lookup({"a": {}}, "a.missing")


def test_boundary_with_no_resolver_uses_the_dotted_path_automatically():
    sent = []
    tools = {"send_message": lambda recipient, text: sent.append((recipient, text))}
    boundary = ToolBoundary(Catalog.from_yaml(FIXTURE), tools)  # no session_resolver at all

    session = {"ticket": {"customer_email": "auto@example.com"}}
    result = boundary.call("send_message", {"recipient": "attacker@evil.com", "text": "hi"}, session)

    assert isinstance(result, Executed)
    assert sent == [("auto@example.com", "hi")]


def test_boundary_with_no_resolver_still_defeats_injection():
    sent = []
    tools = {"send_message": lambda recipient, text: sent.append((recipient, text))}
    boundary = ToolBoundary(Catalog.from_yaml(FIXTURE), tools)

    session = {"ticket": {"customer_email": "real.customer@example.com"}}
    boundary.call("send_message", {"recipient": "backup@evil-collector.com", "text": "hi"}, session)

    assert sent == [("real.customer@example.com", "hi")]
