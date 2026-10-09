"""Stand-in for the existing enterprise ticketing platform (think ServiceNow / Jira SM).

Supports idempotency keys and fault injection (FAILURE_RATE, LATENCY_MS) so the integration layer's retries,
circuit breaker and background re-sync can be exercised locally.
"""

import asyncio
import itertools
import os
import random
from datetime import UTC, datetime

from fastapi import FastAPI, Header, HTTPException

app = FastAPI(title="Legacy Ticketing (mock)")
FAILURE_RATE = float(os.getenv("FAILURE_RATE", "0"))
LATENCY_MS = int(os.getenv("LATENCY_MS", "40"))

_tickets: dict[str, dict] = {}
_idem: dict[str, str] = {}
_seq = itertools.count(50001)


async def _chaos() -> None:
    await asyncio.sleep(LATENCY_MS / 1000)
    if random.random() < FAILURE_RATE:
        raise HTTPException(503, "ticketing backend temporarily unavailable")


@app.post("/api/tickets", status_code=201)
async def create(body: dict, idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    await _chaos()
    if idempotency_key and idempotency_key in _idem:
        return _tickets[_idem[idempotency_key]]
    tid = f"INC{next(_seq)}"
    now = datetime.now(UTC).isoformat()
    _tickets[tid] = {"id": tid, **body, "created_at": now, "updated_at": now, "history": [{"at": now, "event": "created"}]}
    if idempotency_key:
        _idem[idempotency_key] = tid
    return _tickets[tid]


@app.patch("/api/tickets/{tid}")
async def update(tid: str, body: dict):
    await _chaos()
    if tid not in _tickets:
        raise HTTPException(404, "ticket not found")
    now = datetime.now(UTC).isoformat()
    t = _tickets[tid]
    t.update(body)
    t["updated_at"] = now
    t["history"].append({"at": now, "event": f"updated: {', '.join(sorted(body))}"})
    return t


@app.get("/api/tickets/{tid}")
async def get(tid: str):
    await _chaos()
    if tid not in _tickets:
        raise HTTPException(404, "ticket not found")
    return _tickets[tid]


@app.get("/health")
async def health():
    return {"status": "ok", "tickets": len(_tickets)}
