"""Starts a throwaway gateway (in-process fake tool) for the TypeScript tests. Args: port agent_key app_key."""
import sys
from mayi import Catalog, ToolBoundary
from mayi.gateway import Gateway, serve

port, agent, app = int(sys.argv[1]), sys.argv[2], sys.argv[3]
cat = Catalog.from_dict({"refund": {
    "ticket_id": {"pen": "application", "source": "ticket_id"},
    "amount": {"pen": "human", "limit": "amount <= 50"},
    "note": {"pen": "model", "type": "str", "max_len": 80},
}})
b = ToolBoundary(cat, {"refund": lambda **kw: {"refunded": kw["amount"]}})
serve(Gateway(b, agent, app), "127.0.0.1", port).serve_forever()
