from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import AppError
from app.core.security import current_employee, employee_context, issue_dev_token
from app.db.models import Employee
from app.db.session import get_db
from app.services.audit import audit

router = APIRouter(prefix="/auth", tags=["auth"])


class DevLogin(BaseModel):
    employee_id: str


@router.get("/config")
async def auth_config():
    return {"mode": settings.auth_mode, "issuer": settings.oidc_issuer, "llm": "groq" if settings.llm_enabled else "heuristic",
            "models": settings.llm_model_chain if settings.llm_enabled else []}


@router.get("/demo-users")
async def demo_users(db: AsyncSession = Depends(get_db)):
    if settings.auth_mode != "dev":
        raise AppError(404, "not_found", "Demo users are only available in dev auth mode")
    rows = (await db.execute(select(Employee).where(Employee.is_active).order_by(Employee.id))).scalars().all()
    return [employee_context(e) | {"title": e.attributes.get("title")} for e in rows]


@router.post("/dev-login")
async def dev_login(body: DevLogin, db: AsyncSession = Depends(get_db)):
    if settings.auth_mode != "dev":
        raise AppError(404, "not_found", "Dev login is disabled; authenticate through corporate SSO")
    emp = await db.get(Employee, body.employee_id)
    if emp is None or not emp.is_active:
        raise AppError(404, "unknown_employee", "No such active employee")
    audit(db, emp.id, "auth.dev_login", "employee", emp.id)
    await db.commit()
    return {"access_token": issue_dev_token(emp.id), "token_type": "bearer", "employee": employee_context(emp)}


@router.get("/me")
async def me(employee: Employee = Depends(current_employee)):
    return employee_context(employee) | {"title": employee.attributes.get("title")}
