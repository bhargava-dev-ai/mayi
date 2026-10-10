"""
Zero-config resolution for application-pen arguments: `source: "a.b.c"`
in the catalog is looked up on the session by dotted path, trying dict
keys first, then object attributes at each step. Pass your own
session_resolver to ToolBoundary if you need anything more specific;
this is the default when you don't.
"""

from __future__ import annotations

from typing import Any


class SourcePathError(KeyError):
    pass


def dotted_lookup(session: Any, path: str) -> Any:
    node = session
    for part in path.split("."):
        if isinstance(node, dict):
            if part not in node:
                raise SourcePathError(f"'{part}' not found (resolving '{path}')")
            node = node[part]
        elif hasattr(node, part):
            node = getattr(node, part)
        else:
            raise SourcePathError(f"'{part}' not found (resolving '{path}')")
    return node
