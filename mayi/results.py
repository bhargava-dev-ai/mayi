from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Executed:
    tool: str
    args: dict[str, Any]
    result: Any
    executed_as: Optional[str] = None  # set only when a static transform redirected this call to a different tool
    simulated: bool = False  # True for a dry run: nothing executed, result is None


@dataclass
class Rejected:
    tool: str
    reason: str
    detail: dict = field(default_factory=dict)


@dataclass
class Escalated:
    tool: str
    arg: str
    proposed_value: Any
    reason: str = "missing human approval"
