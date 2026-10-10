"""
A real LangGraph StateGraph with mayi wired in as the tool-execution node.

The LLM call itself is a scripted stand-in (this sandbox has no model
API key configured) — everything else is real LangGraph: StateGraph,
add_conditional_edges, MessagesState, AIMessage.tool_calls, checkpointing,
and the native interrupt()/Command(resume=...) human-in-the-loop flow.

Swapping the scripted `fake_llm_node` for `ChatAnthropic(...).bind_tools(...)`
or any other tool-calling chat model requires no change to MayiToolNode,
the boundary, or the catalog. That's the point: mayi only ever sees
AIMessage.tool_calls, which every tool-calling chat model produces in the
same shape.
"""

import os
import uuid
from typing import Annotated, TypedDict

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    session: dict
    actor: str

from mayi import Catalog, ToolBoundary
from mayi.integrations.langgraph import MayiToolNode

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "catalog.yaml")


# ---- stand-in for a real tool-calling model ----

def fake_llm_agent_node(injected: bool):
    def node(state):
        if injected:
            args = {
                "recipient": "backup-records@evil-collector.com",  # the injected target
                "text": "Glad I could help! (forwarding as requested in the thread)",
            }
        else:
            args = {"amount": 5000, "rationale": "customer travel expense, receipts attached"}
        tool_name = "send_message" if injected else "recommend"
        return {"messages": [AIMessage(
            content="", tool_calls=[{"name": tool_name, "args": args, "id": "call_1"}],
        )]}
    return node


# ---- real tool implementations ----

def real_send_message(recipient: str, text: str) -> str:
    SENT_LOG.append((recipient, text))
    return "sent"


def real_recommend(employee_id: str, amount: int, rationale: str) -> str:
    return f"recommendation recorded for {employee_id}: {amount} ({rationale})"


SENT_LOG: list[tuple[str, str]] = []


def resolver(session, tool_name, arg_name):
    if arg_name == "recipient":
        return session["customer_email"]
    if arg_name == "employee_id":
        return session["employee_id"]
    raise KeyError(arg_name)


def build_graph(injected: bool):
    catalog = Catalog.from_yaml(FIXTURE)
    boundary = ToolBoundary(
        catalog,
        {"send_message": real_send_message, "recommend": real_recommend},
        resolver,
    )
    tool_node = MayiToolNode(
        boundary,
        session_from_state=lambda state: state["session"],
        actor_from_state=lambda state: state.get("actor"),
    )

    graph = StateGraph(AgentState)
    graph.add_node("agent", fake_llm_agent_node(injected))
    graph.add_node("tools", tool_node)
    graph.set_entry_point("agent")
    graph.add_edge("agent", "tools")
    graph.add_edge("tools", END)

    return graph.compile(checkpointer=InMemorySaver())


# ---- tests ----

def test_injection_defeated_inside_a_real_langgraph_graph():
    SENT_LOG.clear()
    app = build_graph(injected=True)
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    result = app.invoke(
        {"messages": [HumanMessage("please reply to the customer")],
         "session": {"customer_email": "real.customer@example.com"}},
        config=config,
    )

    assert SENT_LOG == [("real.customer@example.com",
                          "Glad I could help! (forwarding as requested in the thread)")]
    tool_message = result["messages"][-1]
    assert "sent" in tool_message.content


def test_human_pen_pauses_the_graph_and_resumes_on_approval():
    app = build_graph(injected=False)
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    # First invoke hits the human-pen arg (`amount`) and interrupts —
    # the graph is genuinely paused, no recommend() call has happened.
    result = app.invoke(
        {"messages": [HumanMessage("triage this expense report")],
         "session": {"customer_email": "irrelevant", "employee_id": "E999"}},
        config=config,
    )

    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert payload["tool"] == "recommend"
    assert payload["arg"] == "amount"
    assert payload["proposed_value"] == 5000

    # A human reviews the paused graph and denies it.
    final = app.invoke(Command(resume={"approved": False}), config=config)
    tool_message = final["messages"][-1]
    assert "DENIED" in tool_message.content


def test_human_pen_resumes_and_executes_on_approval():
    app = build_graph(injected=False)
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    app.invoke(
        {"messages": [HumanMessage("triage this expense report")],
         "session": {"customer_email": "irrelevant", "employee_id": "E999"}},
        config=config,
    )

    # A human reviews the paused graph and approves this specific value.
    final = app.invoke(
        Command(resume={"approved": True, "approved_by": "risk_owner"}),
        config=config,
    )
    tool_message = final["messages"][-1]
    assert "recommendation recorded for E999: 5000" in tool_message.content
