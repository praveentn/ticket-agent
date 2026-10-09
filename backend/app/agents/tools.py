"""Approved tool catalog.

Every tool declares: a Pydantic argument schema (input validation), the roles allowed to call it (RBAC),
a sensitivity rule (whether a human must approve), and who approves. Handlers only ever act on the requesting
employee's own records (data isolation).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations import department_apis as apis
from app.services import tickets as ticket_svc


@dataclass
class ToolContext:
    db: AsyncSession
    employee: dict
    agent_id: str
    queue: str
    conversation_id: str
    message: str


class ToolDenied(Exception):
    pass


# ---- Argument schemas ----------------------------------------------------------------------------------
class NoArgs(BaseModel):
    pass


class SystemArgs(BaseModel):
    system: str = Field("Corporate SSO", description="Which system/account, e.g. Corporate SSO, VPN, Outlook, SAP")


class SoftwareArgs(BaseModel):
    software_name: str = Field(..., min_length=2, description="Name of the software or license requested")
    justification: str = Field("Requested via support assistant", description="Business justification")


class AdminAccessArgs(BaseModel):
    system: str = Field(..., min_length=2, description="System that needs elevated/admin access")
    duration_hours: int = Field(8, ge=1, le=72, description="How long access is needed, in hours (max 72)")


class LeaveArgs(BaseModel):
    leave_type: Literal["annual", "sick", "casual", "parental", "unpaid"] = Field("annual")
    start_date: str = Field(..., description="First day of leave, ISO format YYYY-MM-DD")
    days: int = Field(..., ge=1, le=60, description="Number of leave days")

    @field_validator("start_date")
    @classmethod
    def _iso(cls, v: str) -> str:
        d = date.fromisoformat(v)
        if d < date.today():
            raise ValueError("start_date cannot be in the past")
        return v


class ExpenseArgs(BaseModel):
    amount: float = Field(..., gt=0, le=50000, description="Claim amount")
    currency: Literal["USD", "EUR", "GBP", "INR"] = Field("USD")
    category: Literal["travel", "meals", "lodging", "transport", "equipment", "training", "other"] = Field("other")
    description: str = Field(..., min_length=3, description="What the expense was for")


class ClaimStatusArgs(BaseModel):
    claim_id: str | None = Field(None, description="Claim reference like EXP-XXXX; latest claim if omitted")


class RoomArgs(BaseModel):
    capacity: int = Field(4, ge=1, le=100, description="Number of attendees")
    date: str = Field(..., description="Booking date YYYY-MM-DD")
    time: str = Field(..., pattern=r"^\d{1,2}:\d{2}$", description="Start time HH:MM (24h)")


class CreateTicketArgs(BaseModel):
    title: str = Field(..., min_length=5, max_length=200, description="Short summary of the issue")
    description: str = Field(..., min_length=5, description="Details of the issue")
    priority: Literal["P1", "P2", "P3", "P4"] = Field("P3")


class TicketStatusArgs(BaseModel):
    ticket_number: str = Field(..., pattern=r"(?i)^(TKT-)?\d+$", description="Ticket number like TKT-1001")


# ---- Handlers ------------------------------------------------------------------------------------------
async def _reset_password(ctx: ToolContext, a: SystemArgs) -> dict:
    return await apis.identity_reset_password(ctx.employee["id"], a.system)


async def _unlock_account(ctx: ToolContext, a: SystemArgs) -> dict:
    return await apis.identity_unlock_account(ctx.employee["id"], a.system)


async def _request_software(ctx: ToolContext, a: SoftwareArgs) -> dict:
    return await apis.itsm_request_software(ctx.employee["id"], a.software_name, a.justification)


async def _grant_admin(ctx: ToolContext, a: AdminAccessArgs) -> dict:
    return await apis.iam_grant_admin_access(ctx.employee["id"], a.system, a.duration_hours)


async def _leave_balance(ctx: ToolContext, a: NoArgs) -> dict:
    return await apis.hris_leave_balance(ctx.employee["id"])


async def _submit_leave(ctx: ToolContext, a: LeaveArgs) -> dict:
    return await apis.hris_submit_leave(ctx.employee["id"], a.leave_type, a.start_date, a.days)


async def _bank_details(ctx: ToolContext, a: NoArgs) -> dict:
    return await apis.hris_bank_update_link(ctx.employee["id"])


async def _submit_expense(ctx: ToolContext, a: ExpenseArgs) -> dict:
    return await apis.finance_submit_expense(ctx.employee["id"], a.amount, a.currency, a.category, a.description)


async def _claim_status(ctx: ToolContext, a: ClaimStatusArgs) -> dict:
    return await apis.finance_reimbursement_status(ctx.employee["id"], a.claim_id)


async def _book_room(ctx: ToolContext, a: RoomArgs) -> dict:
    return await apis.facilities_book_room(ctx.employee["id"], a.capacity, a.date, a.time)


async def _create_ticket(ctx: ToolContext, a: CreateTicketArgs) -> dict:
    t = await ticket_svc.create_ticket(
        ctx.db, employee_id=ctx.employee["id"], department=ctx.agent_id, category="incident", title=a.title,
        description=a.description, priority=a.priority, queue=ctx.queue, conversation_id=ctx.conversation_id,
    )
    return {"ticket": ticket_svc.ticket_dict(t)}


async def _ticket_status(ctx: ToolContext, a: TicketStatusArgs) -> dict:
    t = await ticket_svc.find_by_number(ctx.db, a.ticket_number)
    if t is None:
        return {"found": False, "ticket_number": a.ticket_number}
    if t.employee_id != ctx.employee["id"] and ctx.employee["role"] not in SUPPORT_ROLES:
        raise ToolDenied("You can only view tickets you raised.")  # data isolation
    return {"found": True, "ticket": ticket_svc.ticket_dict(t)}


SUPPORT_ROLES = {"it_admin", "hr_partner", "finance_analyst", "support_agent"}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], Awaitable[dict]]
    allowed_roles: frozenset[str] | None = None  # None = any authenticated employee
    sensitive: Callable[[BaseModel], bool] = lambda _a: False
    approver: str = "manager"
    sensitivity_reason: str = ""
    irreversible: bool = False

    def openai_schema(self) -> dict:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": schema}}


TOOLS: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("reset_password", "Send the employee a self-service password reset link for a system.", SystemArgs, _reset_password),
    ToolSpec("unlock_account", "Unlock the employee's locked account on a system.", SystemArgs, _unlock_account),
    ToolSpec("request_software", "Request installation/license of a software product.", SoftwareArgs, _request_software),
    ToolSpec(
        "grant_admin_access", "Grant time-bound administrator/elevated access to a system.", AdminAccessArgs, _grant_admin,
        sensitive=lambda _a: True, approver="manager", sensitivity_reason="Privileged access always needs approval",
    ),
    ToolSpec("get_leave_balance", "Look up the employee's current leave balances.", NoArgs, _leave_balance),
    ToolSpec(
        "submit_leave_request", "Submit a leave request for the employee.", LeaveArgs, _submit_leave,
        sensitive=lambda a: a.days > 10, approver="manager", sensitivity_reason="Leave longer than 10 days",
    ),
    ToolSpec(
        "update_bank_details", "Start the secure bank-details change process (sends a verified form).", NoArgs, _bank_details,
        sensitive=lambda _a: True, approver="hr_partner", sensitivity_reason="Payroll bank changes are fraud-sensitive",
        irreversible=True,
    ),
    ToolSpec(
        "submit_expense_claim", "Submit an expense reimbursement claim.", ExpenseArgs, _submit_expense,
        sensitive=lambda a: a.amount > 1000, approver="manager", sensitivity_reason="Claims above 1,000 need approval",
    ),
    ToolSpec("get_reimbursement_status", "Check the status of an expense reimbursement claim.", ClaimStatusArgs, _claim_status),
    ToolSpec("book_meeting_room", "Book a meeting room.", RoomArgs, _book_room),
    ToolSpec("create_ticket", "Create a support ticket for an issue that needs a human to fix.", CreateTicketArgs, _create_ticket),
    ToolSpec("get_ticket_status", "Look up the status of an existing support ticket.", TicketStatusArgs, _ticket_status),
]}
