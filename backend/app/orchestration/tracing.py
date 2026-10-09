"""Step-level observability for the workflow.

Every node is wrapped by `traced`, which emits `step` events (start / end / error / waiting) through
LangGraph's custom stream. The API relays them to the UI's workflow pane over SSE, persists them on the
AgentExecution row, and records node latency in Prometheus.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any

from langgraph.config import get_stream_writer
from langgraph.errors import GraphInterrupt

from app.config import settings
from app.core.metrics import NODE_LATENCY

log = logging.getLogger(__name__)

# Node catalogue: drives both the trace labels and the workflow map the UI draws.
NODES: dict[str, dict[str, str]] = {
    "intake": {"label": "Receive message", "group": "orchestrator",
               "explain": "Loads the message, attachments (extracted text/OCR) and recent conversation history into state."},
    "authenticate": {"label": "Authenticate & validate", "group": "orchestrator",
                     "explain": "Confirms the employee identity from the SSO token and validates input size, attachments and scan status."},
    "guardrails": {"label": "Guardrails", "group": "orchestrator",
                   "explain": "Detects prompt injection, redacts PII before any LLM call, and flags sensitive topics."},
    "classify": {"label": "Classify request", "group": "orchestrator",
                 "explain": "Identifies intent, department, urgency, risk, entities and required action, with a confidence score."},
    "route": {"label": "Route", "group": "orchestrator",
              "explain": "Chooses the path: department agent, clarifying question, small talk, or direct human escalation."},
    "clarify": {"label": "Ask clarifying question", "group": "orchestrator",
                "explain": "The request is ambiguous or missing details, so the agent asks instead of guessing."},
    "plan": {"label": "Plan", "group": "agent",
             "explain": "The department agent decides: answer from knowledge (RAG) or perform an action with an approved tool."},
    "retrieve": {"label": "Retrieve knowledge", "group": "knowledge",
                 "explain": "Hybrid search (pgvector + full-text, RRF fusion) over this agent's collections, filtered by document permissions."},
    "rerank": {"label": "Rerank", "group": "knowledge",
               "explain": "Re-scores candidates by semantic similarity, fusion rank and query-term coverage; keeps the best few."},
    "generate": {"label": "Generate grounded answer", "group": "knowledge",
                 "explain": "Writes an answer only from the retrieved sources, citing each fact as [n]."},
    "validate_action": {"label": "Validate tool input", "group": "action",
                        "explain": "Checks the tool exists for this agent and the arguments pass its schema; asks for missing fields."},
    "authorize": {"label": "Authorize", "group": "action",
                  "explain": "Applies RBAC/ABAC policy and decides whether the action is sensitive enough to need human approval."},
    "approval": {"label": "Human approval", "group": "action",
                 "explain": "Pauses the graph (LangGraph interrupt, checkpointed in Postgres) until an approver decides."},
    "execute": {"label": "Execute tool", "group": "action",
                "explain": "Calls the department API with an idempotency key; replays are served from the invocation log."},
    "verify": {"label": "Verify & score", "group": "orchestrator",
               "explain": "Checks citations/tool results, computes final confidence and decides resolved vs escalate."},
    "escalate": {"label": "Escalate to human", "group": "orchestrator",
                 "explain": "Hands off to the department's human queue with the full conversation and ticket context."},
    "respond": {"label": "Respond & update ticket", "group": "orchestrator",
                "explain": "Composes the reply, creates/updates the ticket, syncs the ticketing platform and writes the audit trail."},
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def emit(event: dict[str, Any]) -> None:
    try:
        get_stream_writer()(event)
    except Exception:  # outside a streaming run (e.g. unit tests)
        pass


def _summarize(update: dict[str, Any]) -> dict[str, Any]:
    """Trim large values so the trace stays readable."""
    out: dict[str, Any] = {}
    for k, v in update.items():
        if k in ("history",):
            continue
        if isinstance(v, str) and len(v) > 600:
            out[k] = v[:600] + "…"
        elif isinstance(v, list) and k in ("retrieved", "sources"):
            out[k] = [
                {kk: (vv[:240] + "…" if isinstance(vv, str) and len(vv) > 240 else vv) for kk, vv in item.items()}
                for item in v
            ]
        else:
            out[k] = v
    return out


def traced(node_id: str, *, timeout: bool = True):
    """Wraps a node: emits start/end events, enforces a timeout and converts failures into state (fail-soft)."""
    meta = NODES[node_id]

    def deco(fn: Callable[[dict], Awaitable[dict]]):
        @wraps(fn)
        async def wrapper(state: dict) -> dict:
            agent = state.get("agent") if meta["group"] != "orchestrator" else None
            emit({"type": "step", "phase": "start", "node": node_id, "label": meta["label"], "group": meta["group"],
                  "agent": agent, "ts": _now()})
            start = time.perf_counter()
            try:
                if timeout:
                    async with asyncio.timeout(settings.node_timeout_seconds):
                        update = await fn(state)
                else:
                    update = await fn(state)
            except GraphInterrupt:
                emit({"type": "step", "phase": "waiting", "node": node_id, "label": meta["label"], "group": meta["group"],
                      "agent": agent, "ts": _now(), "summary": "Waiting for human approval"})
                raise
            except Exception as exc:
                ms = int((time.perf_counter() - start) * 1000)
                log.exception("node failed", extra={"node": node_id})
                NODE_LATENCY.labels(node_id).observe(ms / 1000)
                err = {"node": node_id, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                emit({"type": "step", "phase": "error", "node": node_id, "label": meta["label"], "group": meta["group"],
                      "agent": agent, "ts": _now(), "duration_ms": ms, "summary": err["error"]})
                return {"errors": [err]}
            ms = int((time.perf_counter() - start) * 1000)
            NODE_LATENCY.labels(node_id).observe(ms / 1000)
            summary = update.pop("_summary", "")
            emit({"type": "step", "phase": "end", "node": node_id, "label": meta["label"], "group": meta["group"],
                  "agent": agent, "ts": _now(), "duration_ms": ms, "summary": summary, "data": _summarize(update)})
            return update

        return wrapper

    return deco
