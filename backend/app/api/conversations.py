from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat import pending_interrupt
from app.core.errors import AppError
from app.core.security import current_employee
from app.db.models import AgentExecution, Attachment, Conversation, Employee, Message, Ticket
from app.db.session import get_db
from app.services.tickets import ticket_dict

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("")
async def list_conversations(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    employee: Employee = Depends(current_employee), db: AsyncSession = Depends(get_db),
):
    base = select(Conversation).where(Conversation.employee_id == employee.id)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(base.order_by(Conversation.updated_at.desc())
                             .offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"items": [{"id": c.id, "title": c.title, "status": c.status, "updated_at": c.updated_at.isoformat(),
                       "created_at": c.created_at.isoformat()} for c in rows],
            "page": page, "page_size": page_size, "total": total}


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str, employee: Employee = Depends(current_employee),
                           db: AsyncSession = Depends(get_db)):
    c = await db.get(Conversation, conversation_id)
    if c is None or c.employee_id != employee.id:
        raise AppError(404, "not_found", "Conversation not found")
    msgs = (await db.execute(select(Message).where(Message.conversation_id == c.id)
                             .order_by(Message.created_at))).scalars().all()
    att_ids = {a for m in msgs for a in (m.attachment_ids or [])}
    atts = {}
    if att_ids:
        atts = {a.id: {"id": a.id, "filename": a.filename, "content_type": a.content_type}
                for a in (await db.execute(select(Attachment).where(Attachment.id.in_(att_ids)))).scalars()}
    tickets = (await db.execute(select(Ticket).where(Ticket.conversation_id == c.id)
                                .order_by(Ticket.created_at))).scalars().all()
    pending = (await db.execute(select(AgentExecution).where(
        AgentExecution.conversation_id == c.id, AgentExecution.status == "interrupted"))).scalars().first()
    approval = None
    if pending:
        approval = {"execution_id": pending.id, "payload": await pending_interrupt(pending.id)}
    return {
        "id": c.id, "title": c.title, "status": c.status,
        "messages": [{"id": m.id, "role": m.role, "content": m.content, "citations": m.citations,
                      "attachments": [atts[a] for a in (m.attachment_ids or []) if a in atts],
                      "execution_id": m.execution_id, "created_at": m.created_at.isoformat()} for m in msgs],
        "tickets": [ticket_dict(t) for t in tickets],
        "pending_approval": approval,
        "last_execution_id": pending.id if pending else next((m.execution_id for m in reversed(msgs) if m.execution_id), None),
    }
