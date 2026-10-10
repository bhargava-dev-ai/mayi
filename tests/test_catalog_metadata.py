import os

import pytest

from mayi import Catalog, CatalogError, ToolBoundary

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "catalog.yaml")


def resolver(session, tool_name, arg_name):
    return session[arg_name]


def test_metadata_loads_from_the_fixture():
    catalog = Catalog.from_yaml(FIXTURE)
    assert catalog.version == "1"
    assert catalog.owner == "bhargava"
    assert catalog.reviewed_by == "yair.sabag"
    assert catalog.reviewed_on == "2026-09-29"


def test_catalog_with_no_meta_at_all_is_fine():
    catalog = Catalog.from_dict({"t": {"x": {"pen": "model", "type": "str"}}})
    assert catalog.version is None
    assert catalog.owner is None


def test_half_filled_meta_is_rejected():
    with pytest.raises(CatalogError, match="owner.*reviewed_by"):
        Catalog.from_dict({"_meta": {"owner": "someone"}, "t": {"x": {"pen": "model", "type": "str"}}})


def test_meta_appears_in_every_audit_record():
    records = []
    catalog = Catalog.from_yaml(FIXTURE)
    boundary = ToolBoundary(
        catalog, {"send_message": lambda recipient, text: None},
        session_resolver=lambda s, t, a: s.get(a), audit_log=records.append,
    )
    boundary.call("send_message", {"recipient": "x", "text": "hi"}, {"recipient": "real@example.com"})

    assert records[0]["catalog_version"] == "1"
    assert records[0]["catalog_reviewed_by"] == "yair.sabag"
