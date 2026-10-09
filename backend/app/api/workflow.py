from functools import lru_cache

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import current_employee
from app.db.models import AgentExecution, Employee, Feedback
from app.db.session import get_db
from app.orchestration.graph import workflow_map

router = APIRouter(tags=["workflow"])


@lru_cache
def _map() -> dict:
    return workflow_map()


@router.get("/workflow/graph")
async def graph(_: Employee = Depends(current_employee)):
    return _map()


@router.get("/stats")
async def stats(_: Employee = Depends(current_employee), db: AsyncSession = Depends(get_db)):
    """Operational KPIs from spec §6: resolution & escalation rates, routing mix, latency, token use."""
    rows = (await db.execute(select(AgentExecution.outcome, func.count()).where(AgentExecution.status == "completed")
                             .group_by(AgentExecution.outcome))).all()
    outcomes = {o or "unknown": n for o, n in rows}
    total = sum(outcomes.values()) or 1
    agg = (await db.execute(select(
        func.avg(AgentExecution.latency_ms), func.percentile_cont(0.95).within_group(AgentExecution.latency_ms),
        func.sum(AgentExecution.tokens_in), func.sum(AgentExecution.tokens_out),
    ).where(AgentExecution.status == "completed"))).one()
    by_agent = dict((await db.execute(select(AgentExecution.agent, func.count()).where(AgentExecution.agent.isnot(None))
                                      .group_by(AgentExecution.agent))).all())
    fb = (await db.execute(select(func.count(), func.count().filter(Feedback.helpful.is_(True))))).one()
    return {
        "executions": sum(outcomes.values()), "outcomes": outcomes,
        "resolution_rate": round((outcomes.get("answered", 0) + outcomes.get("action_executed", 0)) / total, 3),
        "escalation_rate": round(outcomes.get("escalated", 0) / total, 3),
        "avg_latency_ms": int(agg[0] or 0), "p95_latency_ms": int(agg[1] or 0),
        "tokens_in": int(agg[2] or 0), "tokens_out": int(agg[3] or 0),
        "by_agent": by_agent, "feedback": {"total": fb[0], "helpful": fb[1]},
    }
