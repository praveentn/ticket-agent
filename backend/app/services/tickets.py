"""Ticket lifecycle. PostgreSQL is authoritative; the legacy ticketing platform is kept in sync."""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Ticket
from app.integrations import ticketing
from app.services.audit import audit

log = logging.getLogger(__name__)

OPEN_STATES = ("open", "in_progress", "pending_approval", "pending_info", "escalated")


def ticket_dict(t: Ticket) -> dict[str, Any]:
    return {
        "id": t.id, "number": t.display_id, "external_id": t.external_id, "sync_status": t.sync_status,
        "department": t.department, "category": t.category, "title": t.title, "priority": t.priority,
        "status": t.status, "assigned_queue": t.assigned_queue, "resolution": t.resolution,
        "escalation_reason": t.escalation_reason, "conversation_id": t.conversation_id,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


async def sync_external(db: AsyncSession, ticket: Ticket, enqueue_retry: bool = True) -> None:
    """Best-effort push; on failure the ticket is flagged and a background worker retries."""
    payload = {
        "title": ticket.title, "description": ticket.description, "priority": ticket.priority,
        "status": ticket.status, "queue": ticket.assigned_queue, "category": ticket.category,
        "requester": ticket.employee_id, "internal_ref": ticket.display_id, "resolution": ticket.resolution,
    }
    try:
        if ticket.external_id:
            await ticketing.update_external_ticket(ticket.external_id, payload)
        else:
            created = await ticketing.create_external_ticket(payload, idempotency_key=f"tkt-{ticket.id}")
            ticket.external_id = created["id"]
        ticket.sync_status = "synced"
    except ticketing.TicketingError as exc:
        ticket.sync_status = "failed"
        log.warning("ticket sync failed", extra={"ticket": ticket.display_id, "error": str(exc)})
        if not enqueue_retry:
            return
        try:
            from app.workers.celery_app import celery_app

            celery_app.send_task("tasks.sync_ticket", args=[ticket.id], countdown=30)
        except Exception:  # broker down: the periodic resync still catches it
            log.exception("could not enqueue ticket resync")


async def create_ticket(
    db: AsyncSession, *, employee_id: str, department: str, category: str, title: str, description: str,
    priority: str, queue: str, conversation_id: str | None, status: str = "open",
    resolution: str | None = None, escalation_reason: str | None = None,
) -> Ticket:
    ticket = Ticket(
        employee_id=employee_id, department=department, category=category, title=title[:300],
        description=description, priority=priority, assigned_queue=queue, conversation_id=conversation_id,
        status=status, resolution=resolution, escalation_reason=escalation_reason,
    )
    db.add(ticket)
    await db.flush()
    await db.refresh(ticket)
    await sync_external(db, ticket)
    audit(db, employee_id, "ticket.created", "ticket", ticket.display_id, queue=queue, priority=priority)
    return ticket


async def update_ticket(db: AsyncSession, ticket: Ticket, actor: str, **fields: Any) -> Ticket:
    changed = {k: v for k, v in fields.items() if getattr(ticket, k) != v}
    for k, v in changed.items():
        setattr(ticket, k, v)
    if changed:
        await db.flush()
        await db.refresh(ticket)  # load server-side updated_at without lazy IO
        await sync_external(db, ticket)
        audit(db, actor, "ticket.updated", "ticket", ticket.display_id, changes=list(changed))
    return ticket


async def conversation_ticket(db: AsyncSession, conversation_id: str, department: str | None = None) -> Ticket | None:
    q = select(Ticket).where(Ticket.conversation_id == conversation_id)
    if department:
        q = q.where(Ticket.department == department)
    return (await db.execute(q.order_by(Ticket.created_at.desc()).limit(1))).scalar_one_or_none()


async def find_by_number(db: AsyncSession, number: str) -> Ticket | None:
    digits = "".join(ch for ch in number if ch.isdigit())
    if not digits:
        return None
    return (await db.execute(select(Ticket).where(Ticket.number == int(digits)))).scalar_one_or_none()
