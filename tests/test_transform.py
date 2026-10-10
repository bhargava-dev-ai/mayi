"""
The transform table: a fixed, human-authored redirect from one tool name
to another, decided in advance. Not a rewrite engine, not intent-aware,
not computed from risk -- a lookup table a person wrote down. The
canonical case: the model is never allowed to actually delete a user,
but it can trigger a deletion *request* instead.
"""

import pytest

from mayi import Catalog, CatalogError, Executed, ToolBoundary


def resolver(session, tool_name, arg_name):
    raise KeyError(arg_name)


def make_catalog():
    # The destination of a transform is itself a real, reachable tool
    # (strict mode requires every reachable tool to be catalogued, and a
    # transform target is no exception), so it gets its own entry too --
    # governed the same way, in case it is ever also called directly.
    return Catalog.from_dict({
        "delete_user": {"user_id": {"pen": "model", "type": "str"}},
        "request_user_deletion": {"user_id": {"pen": "model", "type": "str"}},
    })


def test_transform_redirects_execution_but_not_argument_authority():
    calls = []
    boundary = ToolBoundary(
        make_catalog(),
        {
            "delete_user": lambda user_id: calls.append(("delete_user", user_id)),
            "request_user_deletion": lambda user_id: calls.append(("request_user_deletion", user_id)),
        },
        resolver,
        transforms={"delete_user": "request_user_deletion"},
    )

    result = boundary.call("delete_user", {"user_id": "u-42"}, session=None)

    assert isinstance(result, Executed)
    assert result.tool == "delete_user"
    assert result.executed_as == "request_user_deletion"
    assert calls == [("request_user_deletion", "u-42")]  # delete_user itself never ran


def test_no_transform_means_the_tool_runs_as_named():
    calls = []
    boundary = ToolBoundary(
        make_catalog(),
        {"delete_user": lambda user_id: calls.append(user_id)},
        resolver,
    )

    result = boundary.call("delete_user", {"user_id": "u-42"}, session=None)

    assert isinstance(result, Executed)
    assert result.executed_as is None
    assert calls == ["u-42"]


def test_transform_to_unregistered_destination_fails_at_construction():
    with pytest.raises(CatalogError, match="destination is not a registered tool"):
        ToolBoundary(
            make_catalog(),
            {"delete_user": lambda user_id: None},
            resolver,
            transforms={"delete_user": "request_user_deletion"},  # never registered
        )


def test_transform_still_enforces_the_source_tools_pens():
    # The model still cannot smuggle an undeclared argument through a
    # transformed call -- the redirect changes what executes, not what
    # gets past the gate.
    boundary = ToolBoundary(
        make_catalog(),
        {
            "delete_user": lambda user_id: None,
            "request_user_deletion": lambda user_id: None,
        },
        resolver,
        transforms={"delete_user": "request_user_deletion"},
    )

    from mayi import Rejected

    result = boundary.call("delete_user", {"user_id": "u-42", "force": True}, session=None)

    assert isinstance(result, Rejected)
