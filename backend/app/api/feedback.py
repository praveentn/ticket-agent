from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.metrics import ESCALATIONS
from app.core.security import current_employee
from app.db.models import Conversation, Employee, Feedback, Message
from app.db.session import get_db
from app.services import tickets as svc

router = APIRouter(prefix="/feedback", tags=["feedback"])


class FeedbackIn(BaseModel):
    message_id: str
    helpful: bool | None = None
    resolved: bool | None = None
    comment: str | None = Field(None, max_length=2000)


@router.post("", status_code=201)
async def submit(body: FeedbackIn, employee: Employee = Depends(current_employee), db: AsyncSession = Depends(get_db)):
    msg = await db.get(Message, body.message_id)
    convo = await db.get(Conversation, msg.conversation_id) if msg else None
    if msg is None or convo is None or convo.employee_id != employee.id:
        raise AppError(404, "not_found", "Message not found")

    reason = "employee_reported_unresolved" if body.resolved is False else None
    db.add(Feedback(message_id=msg.id, execution_id=msg.execution_id, employee_id=employee.id, helpful=body.helpful,
                    resolved=body.resolved, comment=body.comment, escalation_reason=reason))
    ticket = await svc.conversation_ticket(db, convo.id)
    if ticket is not None:
        if body.resolved is True:
            await svc.update_ticket(db, ticket, employee.id, status="closed")
            convo.status = "resolved"
        elif body.resolved is False:
            # Unresolved: hand to a human with full context rather than leaving the employee stuck.
            await svc.update_ticket(db, ticket, employee.id, status="escalated", escalation_reason=reason,
                                    description=ticket.description + f"\n\nEmployee feedback: not resolved. {body.comment or ''}")
            convo.status = "escalated"
            ESCALATIONS.labels(reason="employee_reported_unresolved").inc()
    await db.commit()
    return {"ok": True, "ticket": svc.ticket_dict(ticket) if ticket else None}
