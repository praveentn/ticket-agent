"""Simulated department systems (identity, HRIS, finance, facilities).

Each function stands in for a real downstream API call. Results are deterministic per employee so demos
are reproducible. Replace these with real clients behind the same signatures.
"""

import hashlib
import random
from datetime import date, timedelta


def _rng(*parts: str) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:12], 16))


def _ref(prefix: str, *parts: str) -> str:
    return f"{prefix}-{hashlib.sha256('|'.join(parts).encode()).hexdigest()[:8].upper()}"


# ---- Identity / IT -------------------------------------------------------------------------------------
async def identity_reset_password(employee_id: str, system: str) -> dict:
    return {
        "reset_reference": _ref("PWR", employee_id, system, date.today().isoformat()),
        "system": system,
        "delivery": "A one-time reset link was sent to your registered mobile number (expires in 15 minutes).",
    }


async def identity_unlock_account(employee_id: str, system: str) -> dict:
    return {"system": system, "status": "unlocked", "note": "Sign in again; MFA re-enrolment is not required."}


async def itsm_request_software(employee_id: str, software: str, justification: str) -> dict:
    catalog = {"slack", "zoom", "vscode", "visual studio code", "python", "docker desktop", "tableau", "figma", "postman"}
    pre_approved = software.lower() in catalog
    return {
        "request_id": _ref("SWR", employee_id, software),
        "software": software,
        "pre_approved": pre_approved,
        "eta": "Deployed via Software Center within 2 hours." if pre_approved else "Requires license review (2-3 business days).",
    }


async def iam_grant_admin_access(employee_id: str, system: str, duration_hours: int) -> dict:
    return {
        "grant_id": _ref("PAM", employee_id, system),
        "system": system,
        "expires_in_hours": duration_hours,
        "note": "Time-bound privileged access granted through PAM; session is recorded.",
    }


# ---- HR ------------------------------------------------------------------------------------------------
async def hris_leave_balance(employee_id: str) -> dict:
    r = _rng(employee_id, "leave")
    return {"annual": r.randint(4, 21), "sick": r.randint(2, 10), "casual": r.randint(0, 6), "as_of": date.today().isoformat()}


async def hris_submit_leave(employee_id: str, leave_type: str, start_date: str, days: int) -> dict:
    start = date.fromisoformat(start_date)
    return {
        "leave_request_id": _ref("LVR", employee_id, start_date, leave_type),
        "leave_type": leave_type,
        "start_date": start_date,
        "end_date": (start + timedelta(days=days - 1)).isoformat(),
        "status": "submitted_for_manager_approval",
    }


async def hris_bank_update_link(employee_id: str) -> dict:
    return {
        "secure_link_sent": True,
        "note": "A secure, identity-verified form was sent to your corporate email. Bank details are never collected in chat.",
        "case_id": _ref("BNK", employee_id, date.today().isoformat()),
    }


# ---- Finance -------------------------------------------------------------------------------------------
async def finance_submit_expense(employee_id: str, amount: float, currency: str, category: str, description: str) -> dict:
    return {
        "claim_id": _ref("EXP", employee_id, f"{amount:.2f}", category, description[:40]),
        "amount": amount,
        "currency": currency,
        "category": category,
        "status": "submitted",
        "expected_payout": "Next payroll cycle after approval",
    }


async def finance_reimbursement_status(employee_id: str, claim_id: str | None) -> dict:
    r = _rng(employee_id, claim_id or "latest")
    status = r.choice(["approved - payout scheduled", "pending manager approval", "under finance review", "paid"])
    return {"claim_id": claim_id or _ref("EXP", employee_id, "latest"), "status": status, "amount": round(r.uniform(40, 900), 2)}


# ---- Facilities ----------------------------------------------------------------------------------------
async def facilities_book_room(employee_id: str, capacity: int, date_str: str, time_str: str) -> dict:
    r = _rng(employee_id, date_str, time_str)
    room = r.choice(["Everest 4.01", "Kilimanjaro 2.14", "Denali 3.07", "Fuji 1.22"])
    return {"booking_id": _ref("RMB", employee_id, date_str, time_str), "room": room, "capacity": max(capacity, 4), "date": date_str, "time": time_str}
