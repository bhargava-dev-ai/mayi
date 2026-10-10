"""
InjecAgent (Zhan et al., 2024) check: when an agent has been fooled into calling an
attacker tool, does mayi stop the call -- and does it still let the user's own task through?

    git clone --depth 1 https://github.com/uiuc-kang-lab/InjecAgent
    python examples/eval_injecagent.py InjecAgent/data

What this measures, and what it does not. InjecAgent's test cases give the attacker's goal
and the tool it needs. Like the AgentDojo run, this script plays the fooled agent: it makes
the attacker's call directly. It does NOT measure how often a model is fooled (InjecAgent's
paper does that). And because every InjecAgent attack uses a *different tool* from the
user's task, the result for the task-scoped catalog is largely by construction: a tool the
catalog does not list is rejected. The argument-level cases mayi is built for (the same
tool, a different argument) are not what this suite tests.

Two catalogs are tried:
  fail-closed  generated from all 38 toolkits with `generate_catalog` (every argument human)
  task-scoped  only the tool the user asked for is listed (arguments written by the model)
"""
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

from mayi import Catalog, Escalated, Executed, Rejected, ToolBoundary
from mayi.generate import generate_catalog

root = Path(sys.argv[1] if len(sys.argv) > 1 else "InjecAgent/data")
toolkits = json.loads((root / "tools.json").read_text())
all_tools = []
for kit in toolkits:
    for t in kit["tools"]:
        props = {p["name"]: {"type": "string", "description": p.get("description", "")} for p in t["parameters"]}
        all_tools.append({"name": kit["name_for_model"] + t["name"], "description": t["summary"],
                          "input_schema": {"type": "object", "properties": props}})
names = {t["name"] for t in all_tools}
yaml_text, _ = generate_catalog(all_tools)
closed = Catalog.from_dict(yaml.safe_load(yaml_text))
params = {t["name"]: list(t["input_schema"]["properties"]) for t in all_tools}
sink = {n: (lambda **kw: "ran") for n in names}

tally = Counter()
for f in ["dh_base", "dh_enhanced", "ds_base", "ds_enhanced"]:
    for c in json.loads((root / f"test_cases_{f}.json").read_text()):
        user, attackers = c["User Tool"], c["Attacker Tools"]
        try:
            uargs = {k: str(v) for k, v in __import__("ast").literal_eval(c["Tool Parameters"]).items()}
        except (ValueError, SyntaxError):
            uargs = {}
        atk_args = {p: c["Attacker Instruction"][:200] for p in params.get(attackers[0], [])}
        tally["cases"] += 1
        tally["attacker tool not in toolkit list"] += attackers[0] not in names

        # undefended: nothing between the agent and the tools
        tally["undefended: attack ran"] += attackers[0] in names

        # fail-closed catalog
        b = ToolBoundary(closed, dict(sink))
        r = b.call(attackers[0], atk_args, {})
        tally["fail-closed: attack ran"] += isinstance(r, Executed)
        tally["fail-closed: attack held or rejected"] += isinstance(r, (Escalated, Rejected))
        r = b.call(user, uargs, {})
        tally["fail-closed: user task ran with no approval"] += isinstance(r, Executed)
        tally["fail-closed: user task held for a person"] += isinstance(r, Escalated)

        # task-scoped catalog
        scoped = Catalog.from_dict({user: {p: {"pen": "model", "type": "str", "max_len": 5000} for p in (uargs or params[user])}})
        b = ToolBoundary(scoped, {user: sink[user]})
        ran = isinstance(b.call(attackers[0], atk_args, {}), Executed)
        tally["task-scoped: attack ran"] += ran
        if ran:
            print("  attack not stopped by task scoping (same tool as the task):", user, "|", c["Attacker Instruction"][:70])
        tally["task-scoped: attack rejected"] += isinstance(b.call(attackers[0], atk_args, {}), Rejected)
        tally["task-scoped: user task ran"] += isinstance(b.call(user, uargs, {}), Executed)

for k, v in tally.items():
    print(f"{v:5d}  {k}")
