import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.logging import correlation_id_var
from app.core.metrics import HTTP_LATENCY


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Accepts or mints X-Correlation-ID, exposes it to logs/audit, echoes it back, records latency."""

    async def dispatch(self, request: Request, call_next):
        cid = request.headers.get("x-correlation-id") or uuid.uuid4().hex
        token = correlation_id_var.set(cid)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Correlation-ID"] = cid
            return response
        finally:
            route = request.scope.get("route")
            HTTP_LATENCY.labels(request.method, getattr(route, "path", "unmatched"), str(status)).observe(
                time.perf_counter() - start
            )
            correlation_id_var.reset(token)
