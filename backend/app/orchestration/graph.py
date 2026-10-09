"""LangGraph wiring: a controlled orchestrator graph with one compiled subgraph per department agent.

    intake → authenticate → guardrails → classify → route ─┬─ clarify ───────────────┐
                                                          ├─ escalate ──────────────┤
                                                          ├─ (smalltalk) ───────────┤
                                                          └─ agent_<dept> → verify ─┴─ escalate? → respond → END
    agent_<dept>:  plan ─┬─ retrieve → rerank → generate
                         └─ validate_action → authorize → [approval] → execute
"""

from langgraph.graph import END, START, StateGraph

from app.agents.registry import load_agents
from app.orchestration import nodes as n
from app.orchestration.state import SupportState
from app.orchestration.tracing import NODES


def _stop_on_error(state: SupportState) -> bool:
    return bool(state.get("errors"))


def after_plan(state: SupportState) -> str:
    if _stop_on_error(state):
        return "stop"
    return "action" if state.get("plan", {}).get("mode") == "action" else "knowledge"


def after_validate(state: SupportState) -> str:
    return "ok" if state.get("tool_validation", {}).get("ok") and not _stop_on_error(state) else "stop"


def after_authorize(state: SupportState) -> str:
    a = state.get("authorization", {})
    if _stop_on_error(state) or not a.get("allowed"):
        return "stop"
    return "approval" if a.get("requires_approval") else "execute"


def after_approval(state: SupportState) -> str:
    return "execute" if state.get("approval", {}).get("approved") else "stop"


def build_agent_subgraph(agent_id: str):
    g = StateGraph(SupportState)
    g.add_node("plan", n.plan)
    g.add_node("retrieve", n.retrieve)
    g.add_node("rerank", n.rerank_node)
    g.add_node("generate", n.generate)
    g.add_node("validate_action", n.validate_action)
    g.add_node("authorize", n.authorize)
    g.add_node("approval", n.approval)
    g.add_node("execute", n.execute)

    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", after_plan, {"knowledge": "retrieve", "action": "validate_action", "stop": END})
    g.add_edge("retrieve", "rerank")
    g.add_edge("rerank", "generate")
    g.add_edge("generate", END)
    g.add_conditional_edges("validate_action", after_validate, {"ok": "authorize", "stop": END})
    g.add_conditional_edges("authorize", after_authorize, {"approval": "approval", "execute": "execute", "stop": END})
    g.add_conditional_edges("approval", after_approval, {"execute": "execute", "stop": END})
    g.add_edge("execute", END)
    return g.compile(name=f"agent_{agent_id}")


def after_gate(state: SupportState) -> str:
    return "stop" if state.get("outcome") == "rejected" else "next"


def after_route(state: SupportState) -> str:
    r = state.get("route")
    if r == "agent":
        return f"agent_{state['agent']}"
    if r in ("smalltalk",):
        return "respond"
    return r  # clarify | escalate


def after_verify(state: SupportState) -> str:
    return "escalate" if state.get("verification", {}).get("escalate") else "respond"


def build_graph() -> StateGraph:
    agents = load_agents()
    g = StateGraph(SupportState)
    g.add_node("intake", n.intake)
    g.add_node("authenticate", n.authenticate)
    g.add_node("guardrails", n.guardrails)
    g.add_node("classify", n.classify)
    g.add_node("route", n.route)
    g.add_node("clarify", n.clarify)
    g.add_node("verify", n.verify)
    g.add_node("escalate", n.escalate)
    g.add_node("respond", n.respond)
    for agent_id in agents:
        g.add_node(f"agent_{agent_id}", build_agent_subgraph(agent_id))
        g.add_edge(f"agent_{agent_id}", "verify")

    g.add_edge(START, "intake")
    g.add_edge("intake", "authenticate")
    g.add_conditional_edges("authenticate", after_gate, {"next": "guardrails", "stop": "respond"})
    g.add_conditional_edges("guardrails", after_gate, {"next": "classify", "stop": "respond"})
    g.add_edge("classify", "route")
    g.add_conditional_edges(
        "route", after_route,
        {"clarify": "clarify", "escalate": "escalate", "respond": "respond",
         **{f"agent_{a}": f"agent_{a}" for a in agents}},
    )
    g.add_edge("clarify", "respond")
    g.add_conditional_edges("verify", after_verify, {"escalate": "escalate", "respond": "respond"})
    g.add_edge("escalate", "respond")
    g.add_edge("respond", END)
    return g


def workflow_map() -> dict:
    """Static description of the graph for the UI's workflow pane (plus Mermaid for the curious)."""
    agents = load_agents()
    graph = build_graph().compile()
    try:
        mermaid = graph.get_graph(xray=1).draw_mermaid()
    except Exception:
        mermaid = ""
    return {
        "nodes": [{"id": k, **v} for k, v in NODES.items()],
        "orchestrator_pre": ["intake", "authenticate", "guardrails", "classify", "route"],
        "branches": ["clarify", "agent", "escalate"],
        "agent_knowledge": ["plan", "retrieve", "rerank", "generate"],
        "agent_action": ["validate_action", "authorize", "approval", "execute"],
        "orchestrator_post": ["verify", "escalate", "respond"],
        "agents": [{"id": a.id, "label": a.label, "description": a.description, "tools": a.tools,
                    "collections": a.knowledge_collections, "queue": a.support_queue} for a in agents.values()],
        "mermaid": mermaid,
    }
