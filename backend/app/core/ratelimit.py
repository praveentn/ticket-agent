"""Per-employee fixed-window rate limiting in Redis (shared across API replicas)."""

import time

import redis.asyncio as aioredis
from fastapi import Depends

from app.config import settings
from app.core.errors import AppError
from app.core.security import current_employee
from app.db.models import Employee

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def rate_limited_employee(employee: Employee = Depends(current_employee)) -> Employee:
    window = int(time.time() // 60)
    key = f"rl:{employee.id}:{window}"
    try:
        r = get_redis()
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, 70)
    except aioredis.RedisError:
        return employee  # degrade open: rate limiting must not take the chat down
    if count > settings.rate_limit_per_minute:
        raise AppError(429, "rate_limited", f"Limit is {settings.rate_limit_per_minute} messages per minute")
    return employee
