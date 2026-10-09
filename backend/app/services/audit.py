from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import correlation_id_var
from app.db.models import AuditEvent


def audit(
    db: AsyncSession, actor: str, action: str, resource_type: str | None = None, resource_id: str | None = None,
    correlation_id: str | None = None, **details: Any,
) -> None:
    """Adds an audit row to the caller's unit of work (committed with it)."""
    db.add(AuditEvent(
        actor=actor, action=action, resource_type=resource_type, resource_id=resource_id,
        correlation_id=correlation_id or correlation_id_var.get(), details=details,
    ))
