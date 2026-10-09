from typing import Literal

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import load_agents
from app.agents.tools import SUPPORT_ROLES
from app.core.errors import AppError
from app.core.ratelimit import get_redis
from app.core.security import current_employee
from app.db.models import Employee, Ticket
from app.db.session import get_db
from app.integrations import ticketing
from app.services import tickets as svc

router = APIRouter(prefix="/tickets", tags=["tickets"])


class TicketCreate(BaseModel):
    department: str
    title: str = Field(..., min_length=5, max_length=200)
    description: str = Field(..., min_length=5)
    priority: Literal["P1", "P2", "P3", "P4"] = "P3"


class TicketPatch(BaseModel):
    status: Literal["open", "in_progress", "resolved", "closed", "escalated"] | None = None
    priority: Literal["P1", "P2", "P3", "P4"] | None = None
    resolution: str | None = None


def _can_see(t: Ticket, e: Employee) -> bool:
    return t.employee_id == e.id or e.role in SUPPORT_ROLES


@router.get("")
async def list_tickets(
    status: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    employee: Employee = Depends(current_employee), db: AsyncSession = Depends(get_db),
):
    q = select(Ticket)
    if employee.role not in SUPPORT_ROLES:
        q = q.where(Ticket.employee_id == employee.id)
    if status:
        q = q.where(Ticket.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(Ticket.updated_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars()
    return {"items": [svc.ticket_dict(t) for t in rows], "page": page, "page_size": page_size, "total": total}


@router.get("/{number}")
async def get_ticket(number: str, employee: Employee = Depends(current_employee), db: AsyncSession = Depends(get_db)):
    t = await svc.find_by_number(db, number)
    if t is None or not _can_see(t, employee):
        raise AppError(404, "not_found", "Ticket not found")
    out = svc.ticket_dict(t) | {"description": t.description}
    if t.external_id:
        try:
            out["external"] = await ticketing.get_external_ticket(t.external_id)
        except ticketing.TicketingError as exc:
            out["external"] = {"error": str(exc)}
    return out


@router.post("", status_code=201)
async def create_ticket(
    body: TicketCreate, idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    employee: Employee = Depends(current_employee), db: AsyncSession = Depends(get_db),
):
    agents = load_agents()
    if body.department not in agents:
        raise AppError(422, "unknown_department", f"department must be one of {sorted(agents)}")
    redis_key = f"idem:ticket:{employee.id}:{idempotency_key}" if idempotency_key else None
    if redis_key and (existing_id := await get_redis().get(redis_key)):
        t = await db.get(Ticket, existing_id)
        if t:
            return svc.ticket_dict(t)
    t = await svc.create_ticket(db, employee_id=employee.id, department=body.department, category="manual",
                                title=body.title, description=body.description, priority=body.priority,
                                queue=agents[body.department].support_queue, conversation_id=None)
    await db.commit()
    if redis_key:
        await get_redis().set(redis_key, t.id, ex=86400)
    return svc.ticket_dict(t)


@router.patch("/{number}")
async def patch_ticket(number: str, body: TicketPatch, employee: Employee = Depends(current_employee),
                       db: AsyncSession = Depends(get_db)):
    t = await svc.find_by_number(db, number)
    if t is None or not _can_see(t, employee):
        raise AppError(404, "not_found", "Ticket not found")
    changes = body.model_dump(exclude_none=True)
    if employee.role not in SUPPORT_ROLES:
        # Requesters may only close or reopen their own tickets.
        if set(changes) - {"status"} or changes.get("status") not in ("closed", "open"):
            raise AppError(403, "forbidden", "You can only close or reopen your own tickets")
    await svc.update_ticket(db, t, employee.id, **changes)
    await db.commit()
    return svc.ticket_dict(t)
