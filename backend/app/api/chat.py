"""Conversational endpoints. Responses stream as Server-Sent Events: workflow steps, answer tokens, final result."""

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import AppError
from app.core.logging import correlation_id_var
from app.core.ratelimit import rate_limited_employee
from app.core.security import current_employee, employee_context
from app.db.models import AgentExecution, Attachment, Conversation, Employee, Message
from app.db.session import get_db
from app.orchestration import runner
from app.services.audit import audit

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(..., min_length=1)
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)


class ApprovalDecision(BaseModel):
    approved: bool
    comment: str = Field("", max_length=500)


def _sse(events: AsyncIterator[dict], head: list[dict]) -> StreamingResponse:
    async def gen():
        for e in head:
            yield f"data: {json.dumps(e, default=str)}\n\n"
        async for e in events:
            yield f"data: {json.dumps(e, default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest, employee: Employee = Depends(rate_limited_employee), db: AsyncSession = Depends(get_db)
):
    if len(body.message) > settings.max_message_chars:
        raise AppError(422, "message_too_long", f"Messages are limited to {settings.max_message_chars} characters")

    if body.conversation_id:
        convo = await db.get(Conversation, body.conversation_id)
        if convo is None or convo.employee_id != employee.id:
            raise AppError(404, "not_found", "Conversation not found")
        busy = (await db.execute(select(AgentExecution.id).where(
            AgentExecution.conversation_id == convo.id, AgentExecution.status == "interrupted"))).first()
        if busy:
            raise AppError(409, "awaiting_approval", "This conversation is waiting on an approval decision")
    else:
        convo = Conversation(employee_id=employee.id, title=body.message.strip()[:80])
        db.add(convo)
        await db.flush()

    if body.attachment_ids:
        owned = (await db.execute(select(Attachment.id).where(
            Attachment.id.in_(body.attachment_ids), Attachment.employee_id == employee.id))).scalars().all()
        if len(owned) != len(set(body.attachment_ids)):
            raise AppError(404, "attachment_not_found", "One or more attachments were not found")
        for a in (await db.execute(select(Attachment).where(Attachment.id.in_(owned)))).scalars():
            a.conversation_id = convo.id

    msg = Message(conversation_id=convo.id, role="user", content=body.message.strip(), attachment_ids=body.attachment_ids)
    db.add(msg)
    await db.flush()
    ex = AgentExecution(conversation_id=convo.id, employee_id=employee.id, correlation_id=correlation_id_var.get(),
                        user_message_id=msg.id, llm_mode="groq" if settings.llm_enabled else "heuristic")
    db.add(ex)
    audit(db, employee.id, "chat.message", "conversation", convo.id, message_id=msg.id)
    await db.commit()
    await db.refresh(msg)

    graph_input = {
        "correlation_id": correlation_id_var.get(), "execution_id": ex.id, "conversation_id": convo.id,
        "employee": employee_context(employee), "message": msg.content,
        "attachments": [{"id": i} for i in body.attachment_ids], "errors": [], "llm_usage": [],
    }
    queue = runner.start_run(execution_id=ex.id, graph_input=graph_input)
    head = [{"type": "accepted", "conversation_id": convo.id, "conversation_title": convo.title,
             "user_message": {"id": msg.id, "role": "user", "content": msg.content, "attachment_ids": body.attachment_ids,
                              "created_at": msg.created_at.isoformat() if msg.created_at else None},
             "execution_id": ex.id, "correlation_id": correlation_id_var.get()}]
    return _sse(runner.event_stream(queue), head)


async def pending_interrupt(execution_id: str) -> dict | None:
    """Reads the approval request payload from the checkpointed graph state."""
    snapshot = await runner.get_graph().aget_state(runner.thread_config(execution_id))
    for task in snapshot.tasks:
        for intr in getattr(task, "interrupts", None) or ():
            return intr.value
    return None


@router.post("/executions/{execution_id}/approval")
async def decide_approval(
    execution_id: str, body: ApprovalDecision,
    employee: Employee = Depends(current_employee), db: AsyncSession = Depends(get_db),
):
    ex = await db.get(AgentExecution, execution_id)
    if ex is None or ex.status != "interrupted":
        raise AppError(404, "not_found", "No pending approval for this execution")
    payload = await pending_interrupt(execution_id) or {}
    requester = await db.get(Employee, ex.employee_id)
    is_requester = employee.id == ex.employee_id
    is_manager = payload.get("approver_role") == "manager" and requester is not None and requester.manager_id == employee.id
    is_role_approver = payload.get("approver_role") not in (None, "manager") and employee.role == payload["approver_role"]
    if not (is_manager or is_role_approver or (is_requester and settings.allow_self_approval)):
        raise AppError(403, "not_an_approver", "You are not an approver for this request")
    if is_requester and not settings.allow_self_approval:
        raise AppError(403, "self_approval_forbidden", "Requesters cannot approve their own requests")

    ex.status = "running"
    audit(db, employee.id, "approval.decided", "execution", execution_id, approved=body.approved, comment=body.comment,
          self_approval=is_requester)
    await db.commit()

    decision = {"approved": body.approved, "comment": body.comment,
                "approver": f"{employee.name}{' (self-approved: demo mode)' if is_requester else ''}"}
    queue = runner.start_run(execution_id=execution_id, graph_input=decision, resume=True)
    return _sse(runner.event_stream(queue), [{"type": "accepted", "conversation_id": ex.conversation_id,
                                               "execution_id": execution_id}])


@router.get("/executions/{execution_id}")
async def get_execution(execution_id: str, employee: Employee = Depends(current_employee), db: AsyncSession = Depends(get_db)):
    ex = await db.get(AgentExecution, execution_id)
    if ex is None or (ex.employee_id != employee.id and employee.role not in ("it_admin", "support_agent")):
        raise AppError(404, "not_found", "Execution not found")
    return {
        "id": ex.id, "conversation_id": ex.conversation_id, "status": ex.status, "agent": ex.agent, "intent": ex.intent,
        "outcome": ex.outcome, "confidence": ex.confidence, "llm_mode": ex.llm_mode, "steps": ex.steps,
        "tokens_in": ex.tokens_in, "tokens_out": ex.tokens_out, "latency_ms": ex.latency_ms,
        "correlation_id": ex.correlation_id, "created_at": ex.created_at.isoformat(),
    }
