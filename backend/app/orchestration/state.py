"""Typed LangGraph state shared by the orchestrator and the department-agent subgraphs."""

from typing import Annotated, Any, TypedDict


def append_unique(left: list, right: list) -> list:
    """List reducer that ignores items already present.

    A subgraph hands its whole final state back to the parent, so a plain `operator.add` would
    double-count everything the parent passed in.
    """
    return [*left, *(x for x in right if x not in left)]


class SupportState(TypedDict, total=False):
    # Identity & context
    correlation_id: str
    execution_id: str
    conversation_id: str
    employee: dict[str, Any]
    message: str
    attachments: list[dict[str, Any]]
    history: list[dict[str, str]]
    pending_plan: dict[str, Any] | None  # a tool plan from a previous turn still waiting on missing fields

    # Orchestrator outputs
    validation: dict[str, Any]
    guardrail: dict[str, Any]
    redacted_message: str
    classification: dict[str, Any]
    route: str  # clarify | smalltalk | escalate | agent
    agent: str | None

    # Department agent outputs
    plan: dict[str, Any]  # {mode: knowledge|action, tool, args, source}
    retrieved: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    answer: str
    citations: list[dict[str, Any]]
    tool_validation: dict[str, Any]
    authorization: dict[str, Any]
    approval: dict[str, Any]
    tool_result: dict[str, Any]

    # Verification / outcome
    verification: dict[str, Any]
    escalation: dict[str, Any]
    ticket: dict[str, Any] | None
    secondary_tickets: list[dict[str, Any]]
    final_response: str
    outcome: str  # answered | action_executed | clarification | escalated | rejected | smalltalk

    # Accumulated across nodes
    errors: Annotated[list[dict[str, Any]], append_unique]
    llm_usage: Annotated[list[dict[str, Any]], append_unique]
