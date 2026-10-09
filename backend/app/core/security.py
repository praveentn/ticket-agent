"""Authentication.

dev  -> HS256 JWTs issued by /auth/dev-login for seeded demo employees (local only).
oidc -> RS256 tokens from the corporate IdP, verified against its JWKS; the email claim maps to an Employee.
"""

from datetime import UTC, datetime, timedelta
from functools import lru_cache

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import AppError
from app.db.models import Employee
from app.db.session import get_db

bearer = HTTPBearer(auto_error=False)


def issue_dev_token(employee_id: str) -> str:
    now = datetime.now(UTC)
    claims = {"sub": employee_id, "iat": now, "exp": now + timedelta(minutes=settings.jwt_ttl_minutes), "iss": "dev"}
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


@lru_cache
def _jwks_client() -> jwt.PyJWKClient:
    if not settings.oidc_jwks_url:
        raise AppError(500, "auth_misconfigured", "OIDC_JWKS_URL is not configured")
    return jwt.PyJWKClient(settings.oidc_jwks_url)


async def _employee_from_token(token: str, db: AsyncSession) -> Employee:
    try:
        if settings.auth_mode == "oidc":
            key = _jwks_client().get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key, algorithms=["RS256", "ES256"], audience=settings.oidc_audience, issuer=settings.oidc_issuer
            )
            email = claims.get(settings.oidc_email_claim)
            employee = (await db.execute(select(Employee).where(Employee.email == email))).scalar_one_or_none()
        else:
            claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
            employee = await db.get(Employee, claims["sub"])
    except jwt.PyJWTError as exc:
        raise AppError(401, "invalid_token", "Authentication token is invalid or expired") from exc
    if employee is None or not employee.is_active:
        raise AppError(403, "inactive_employee", "No active employee record for this identity")
    return employee


async def current_employee(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: AsyncSession = Depends(get_db)
) -> Employee:
    if creds is None:
        raise AppError(401, "unauthenticated", "Missing bearer token")
    return await _employee_from_token(creds.credentials, db)


def employee_context(e: Employee) -> dict:
    """The authorization attributes the workflow carries in its state (ABAC inputs)."""
    return {
        "id": e.id, "name": e.name, "email": e.email, "role": e.role,
        "department": e.department, "country": e.country, "manager_id": e.manager_id,
    }
