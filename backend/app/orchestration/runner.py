"""Runs one workflow turn in the background and fans its events out to the SSE stream.

The run is decoupled from the HTTP connection: if the browser disconnects, the turn still completes and is
persisted. Each turn gets its own LangGraph thread (checkpointed in Postgres), which is what makes a turn
resumable after a human-approval interrupt — on any API replica.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from langgraph.types import Command

from app.config import settings
from app.core.logging import correlation_id_var
from app.db.models import AgentExecution, Conversation, Message
from app.db.session import session_scope
from app.services import tickets as ticket_svc
from app.services.audit import audit

log = logging.getLogger(__name__)

_graph = None
_background: set[asyncio.Task] = set()
_DONE = object()


def set_graph(graph) -> None:
    global _graph
    _graph = graph


def get_graph():
    if _graph is None:
        raise RuntimeError("workflow graph not initialised")
    return _graph


def thread_config(execution_id: str) -> dict:
    return {"configurable": {"thread_id": execution_id}, "recursion_limit": settings.max_graph_steps}


def start_run(*, execution_id: str, graph_input: Any, resume: bool = False) -> asyncio.Queue:
    """Kicks off the run as a background task and returns the queue its events are written to."""
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(_run(execution_id, graph_input, queue, resume, correlation_id_var.get()))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return queue


async def _run(execution_id: str, graph_input: Any, queue: asyncio.Queue, resume: bool, cid: str) -> None:
    correlation_id_var.set(cid)
    graph = get_graph()
    config = thread_config(execution_id)
    steps: list[dict] = []
    seq = 0
    started = time.perf_counter()

    def push(event: dict) -> None:
        nonlocal seq
        seq += 1
        event = {**event, "seq": seq, "execution_id": execution_id}
        if event.get("type") == "step":
            steps.append(event)
        queue.put_nowait(event)

    push({"type": "run_start" if not resume else "run_resume", "llm_mode": "groq" if settings.llm_enabled else "heuristic",
          "models": settings.llm_model_chain if settings.llm_enabled else []})
    try:
        payload = Command(resume=graph_input) if resume else graph_input
        async for _ns, mode, chunk in graph.astream(payload, config, stream_mode=["custom", "updates"], subgraphs=True):
            if mode == "custom" and isinstance(chunk, dict):
                push(chunk)

        snapshot = await graph.aget_state(config)
        interrupts = [i for t in snapshot.tasks for i in (getattr(t, "interrupts", None) or ())]
        if interrupts:
            await _on_interrupt(execution_id, snapshot.values, interrupts[0].value, steps, started)
            push({"type": "interrupt", "payload": interrupts[0].value})
        else:
            final = await _on_complete(execution_id, snapshot.values, steps, started)
            push({"type": "final", **final})
    except Exception as exc:
        log.exception("workflow run failed", extra={"execution_id": execution_id})
        await _on_failure(execution_id, steps, str(exc))
        push({"type": "error", "message": "The assistant hit an unexpected error. Your message was saved; please retry."})
    finally:
        queue.put_nowait(_DONE)


async def _save_steps(ex: AgentExecution, steps: list[dict]) -> None:
    ex.steps = [*(ex.steps or []), *steps]


async def _on_interrupt(execution_id: str, values: dict, payload: dict, steps: list[dict], started: float) -> None:
    async with session_scope() as db:
        ex = await db.get(AgentExecution, execution_id)
        await _save_steps(ex, steps)
        ex.status, ex.agent = "interrupted", values.get("agent")
        ex.intent = values.get("classification", {}).get("intent")
        # The ticket reflects that a human decision is pending.
        t = await ticket_svc.conversation_ticket(db, values["conversation_id"], values.get("agent"))
        if t is None or t.status in ("resolved", "closed"):
            agent = values.get("agent") or "general"
            from app.agents.registry import load_agents
            queue = load_agents()[agent].support_queue if agent in load_agents() else "Employee Support Center"
            t = await ticket_svc.create_ticket(
                db, employee_id=values["employee"]["id"], department=agent, category=payload["tool"],
                title=values["message"][:120], description=values["message"],
                priority=values.get("classification", {}).get("urgency", "P3"), queue=queue,
                conversation_id=values["conversation_id"], status="pending_approval",
            )
        else:
            await ticket_svc.update_ticket(db, t, values["employee"]["id"], status="pending_approval")
        audit(db, values["employee"]["id"], "approval.requested", "execution", execution_id, tool=payload["tool"],
              approver_role=payload.get("approver_role"))


async def _on_complete(execution_id: str, values: dict, steps: list[dict], started: float) -> dict:
    usage = values.get("llm_usage", [])
    async with session_scope() as db:
        ex = await db.get(AgentExecution, execution_id)
        msg = Message(conversation_id=values["conversation_id"], role="assistant",
                      content=values.get("final_response") or "I'm sorry — I couldn't produce a response.",
                      citations=values.get("citations", []) if values.get("outcome") in ("answered", "escalated") else [],
                      execution_id=execution_id)
        db.add(msg)
        await _save_steps(ex, steps)
        ex.status = "completed"
        ex.agent = values.get("agent")
        ex.intent = values.get("classification", {}).get("intent")
        ex.outcome = values.get("outcome")
        ex.confidence = values.get("verification", {}).get("confidence")
        ex.tokens_in = (ex.tokens_in or 0) + sum(u.get("tokens_in", 0) for u in usage)
        ex.tokens_out = (ex.tokens_out or 0) + sum(u.get("tokens_out", 0) for u in usage)
        ex.latency_ms = (ex.latency_ms or 0) + int((time.perf_counter() - started) * 1000)
        ex.completed_at = datetime.now(UTC)
        convo = await db.get(Conversation, values["conversation_id"])
        if convo:
            convo.updated_at = datetime.now(UTC)
        await db.flush()
        await db.refresh(msg)
        message = {"id": msg.id, "role": "assistant", "content": msg.content, "citations": msg.citations,
                   "execution_id": execution_id, "created_at": msg.created_at.isoformat()}
    return {
        "message": message,
        "outcome": values.get("outcome"),
        "agent": values.get("agent"),
        "confidence": values.get("verification", {}).get("confidence"),
        "ticket": values.get("ticket"),
        "secondary_tickets": values.get("secondary_tickets", []),
        "usage": usage,
    }


async def _on_failure(execution_id: str, steps: list[dict], error: str) -> None:
    try:
        async with session_scope() as db:
            ex = await db.get(AgentExecution, execution_id)
            if ex:
                await _save_steps(ex, steps)
                ex.status, ex.outcome = "failed", "error"
                ex.completed_at = datetime.now(UTC)
    except Exception:
        log.exception("could not persist failed execution")


async def event_stream(queue: asyncio.Queue):
    """Async iterator over events until the run finishes (used by the SSE endpoint)."""
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=15)
        except TimeoutError:
            yield {"type": "ping"}
            continue
        if item is _DONE:
            return
        yield item
