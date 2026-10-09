"""Adapter for the existing (legacy) enterprise ticketing platform.

Bounded retries with jittered backoff, a circuit breaker, timeouts and idempotency keys. Our own PostgreSQL
ticket row stays authoritative; when sync fails the row is marked sync_status=failed and a worker retries it.
"""

import logging
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from app.config import settings
from app.integrations.resilience import CircuitBreaker, CircuitOpen

log = logging.getLogger(__name__)
breaker = CircuitBreaker("ticketing", threshold=5, reset_seconds=30)


class TicketingError(RuntimeError):
    pass


class _Retryable(Exception):
    pass


async def _request(method: str, path: str, *, json: dict | None = None, idempotency_key: str | None = None) -> dict:
    try:
        breaker.before()
    except CircuitOpen as exc:
        raise TicketingError(str(exc)) from exc

    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_random_exponential(multiplier=0.2, max=2),
            retry=retry_if_exception_type(_Retryable),
            reraise=True,
        ):
            with attempt:
                try:
                    async with httpx.AsyncClient(
                        base_url=settings.ticketing_base_url, timeout=settings.ticketing_timeout_seconds
                    ) as client:
                        resp = await client.request(method, path, json=json, headers=headers)
                except httpx.TransportError as exc:
                    raise _Retryable(str(exc)) from exc
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise _Retryable(f"{resp.status_code} {resp.text[:200]}")
                if resp.status_code >= 400:
                    raise TicketingError(f"{resp.status_code} {resp.text[:200]}")
                breaker.success()
                return resp.json()
    except _Retryable as exc:
        breaker.failure()
        raise TicketingError(f"ticketing unavailable after retries: {exc}") from exc
    raise TicketingError("unreachable")


async def create_external_ticket(payload: dict[str, Any], idempotency_key: str) -> dict:
    return await _request("POST", "/api/tickets", json=payload, idempotency_key=idempotency_key)


async def update_external_ticket(external_id: str, patch: dict[str, Any]) -> dict:
    return await _request("PATCH", f"/api/tickets/{external_id}", json=patch)


async def get_external_ticket(external_id: str) -> dict:
    return await _request("GET", f"/api/tickets/{external_id}")
