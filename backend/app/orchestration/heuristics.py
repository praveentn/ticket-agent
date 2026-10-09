"""Deterministic NLU used when no LLM key is configured and as a safety net when the LLM fails.

Transparent on purpose: the trace shows exactly which keywords drove each decision.
"""

import re
from datetime import date, timedelta
from typing import Any

from app.agents.registry import load_agents

# ---- Guardrail patterns --------------------------------------------------------------------------------
INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) (instructions|prompts?)",
    r"disregard (the |your )?(system|previous) (prompt|instructions)",
    r"you are now\b", r"reveal (your|the) (system )?prompt", r"\bjailbreak\b", r"act as (an? )?(admin|root|developer)",
    r"pretend (to be|you are)", r"override (your|the) (rules|policy|guardrails)",
]
PII_PATTERNS: dict[str, str] = {
    "card_number": r"\b(?:\d[ -]?){13,16}\b",
    "us_ssn": r"\b\d{3}-\d{2}-\d{4}\b",
    "iban": r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b",
    "phone": r"(?<!\w)\+?\d[\d -]{8,}\d\b",
    "password_disclosure": r"(?i)\b(my )?(password|passcode|pin) (is|=|:)\s*\S+",
}
SENSITIVE_TOPICS: dict[str, list[str]] = {
    "harassment": ["harass", "bully", "bullied", "discriminat", "hostile work", "sexual"],
    "safety": ["suicid", "self-harm", "self harm", "hurt myself", "unsafe", "threat", "violence", "weapon"],
    "legal": ["lawsuit", "lawyer", "legal action", "sue ", "whistleblow", "ethics violation"],
    "fraud": ["fraud", "embezzl", "bribe", "kickback"],
    "security_incident": ["data breach", "leaked data", "hacked", "ransomware", "compromised account"],
    "termination": ["fired", "terminated", "termination", "laid off", "layoff"],
}
HUMAN_REQUEST = ["speak to a human", "talk to a human", "real person", "human agent", "talk to someone",
                 "speak to someone", "live agent", "speak with a person", "talk to a person", "escalate"]
GREETINGS = {"hi", "hello", "hey", "thanks", "thank you", "good morning", "good afternoon", "ok", "okay", "cool", "great"}

# ---- Intent / action lexicon: (regex, tool, department) -------------------------------------------------
ACTION_RULES: list[tuple[str, str, str]] = [
    (r"\b(reset|forgot|forgotten|change)\b.*\bpassword\b|\bpassword\b.*\b(reset|expired|forgot)", "reset_password", "it"),
    (r"\b(unlock|locked out|account (is )?locked)\b", "unlock_account", "it"),
    (r"\b(admin|administrator|elevated|root|sudo) (access|rights|privileges?)\b", "grant_admin_access", "it"),
    (r"\b(install|need|request|get)\b.{0,30}\b(software|license|licence|app|application|tool)\b|\binstall\b", "request_software", "it"),
    (r"\b(leave|pto|vacation|holiday) balance\b|\bhow many (leave|vacation|pto|sick) days\b|\bdays (of leave )?(do i have|left)\b", "get_leave_balance", "hr"),
    (r"\b(apply|request|submit|book|take)\b.{0,25}\b(leave|pto|vacation|time off|day off|days off)\b", "submit_leave_request", "hr"),
    (r"\b(update|change|new)\b.{0,20}\bbank\b", "update_bank_details", "hr"),
    (r"\b(submit|file|claim|raise)\b.{0,25}\b(expense|reimbursement|claim)\b|\breimburse me\b", "submit_expense_claim", "finance"),
    (r"\b(status|where is|update on)\b.{0,25}\b(reimbursement|claim|expense)\b|\bEXP-[A-Z0-9]+\b", "get_reimbursement_status", "finance"),
    (r"\b(book|reserve)\b.{0,20}\b(room|meeting room|conference room)\b", "book_meeting_room", "facilities"),
    (r"\b(status|update)\b.{0,25}\b(ticket|request)\b|\bTKT-\d+\b", "get_ticket_status", "*"),
    (r"\b(create|raise|open|log|file)\b.{0,15}\b(ticket|incident)\b|\b(not working|broken|doesn't work|does not work|keeps crashing|is down)\b", "create_ticket", "*"),
]
URGENCY = {
    "P1": ["outage", "everyone", "whole team", "entire office", "production down", "security breach", "phishing",
           "ransomware", "malware", "virus", "data breach", "critical"],
    "P2": ["urgent", "asap", "blocked", "can't work", "cannot work", "unable to work", "deadline", "today",
           "not working", "locked out", "immediately"],
    "P4": ["question", "how do i", "what is", "where can i", "curious", "when is", "policy"],
}
QUESTION_STARTS = ("how", "what", "when", "where", "why", "which", "who", "can i", "is there", "do i", "does", "are", "is ")


def detect_injection(text: str) -> list[str]:
    return [p for p in INJECTION_PATTERNS if re.search(p, text, re.I)]


def redact_pii(text: str) -> tuple[str, list[str]]:
    found: list[str] = []
    for label, pattern in PII_PATTERNS.items():
        if re.search(pattern, text):
            found.append(label)
            text = re.sub(pattern, f"[REDACTED_{label.upper()}]", text)
    return text, found


def sensitive_topics(text: str) -> list[str]:
    low = text.lower()
    return [topic for topic, words in SENSITIVE_TOPICS.items() if any(w in low for w in words)]


def wants_human(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in HUMAN_REQUEST)


def score_departments(text: str) -> dict[str, dict[str, Any]]:
    low = f" {text.lower()} "
    out: dict[str, dict[str, Any]] = {}
    for agent in load_agents().values():
        hits = {kw: w for kw, w in agent.keywords.items() if re.search(rf"(?<![a-z]){re.escape(kw)}(?![a-z])", low)}
        out[agent.id] = {"score": float(sum(hits.values())), "matched": sorted(hits)}
    return out


def match_action(text: str) -> tuple[str | None, str | None]:
    for pattern, tool, dept in ACTION_RULES:
        if re.search(pattern, text, re.I):
            return tool, dept
    return None, None


def urgency(text: str) -> str:
    low = text.lower()
    for level in ("P1", "P2", "P4"):
        if any(k in low for k in URGENCY[level]):
            return level
    return "P3"


# ---- Entity extraction ---------------------------------------------------------------------------------
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
SYSTEMS = ["vpn", "outlook", "email", "sso", "sap", "workday", "jira", "github", "salesforce", "teams", "zoom", "slack",
           "laptop", "windows", "okta", "confluence", "aws", "azure", "servicenow", "tableau"]


def _parse_date(text: str, today: date) -> str | None:
    low = text.lower()
    if m := re.search(r"\b(\d{4}-\d{2}-\d{2})\b", low):
        return m.group(1)
    if "day after tomorrow" in low:
        return (today + timedelta(days=2)).isoformat()
    if "tomorrow" in low:
        return (today + timedelta(days=1)).isoformat()
    if "today" in low:
        return today.isoformat()
    for i, wd in enumerate(WEEKDAYS):
        if re.search(rf"\b(next |this |on )?{wd}\b", low):
            delta = (i - today.weekday()) % 7 or 7
            return (today + timedelta(days=delta)).isoformat()
    m = re.search(r"\b(\d{1,2})(st|nd|rd|th)?\s+(of\s+)?(" + "|".join(MONTHS) + r")\b", low) or \
        re.search(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(st|nd|rd|th)?\b", low)
    if m:
        groups = m.groups()
        if groups[0].isdigit():
            day, month = int(groups[0]), MONTHS[groups[3]]
        else:
            month, day = MONTHS[groups[0]], int(groups[1])
        try:
            d = date(today.year, month, day)
            return (d if d >= today else date(today.year + 1, month, day)).isoformat()
        except ValueError:
            return None
    return None


def extract_entities(text: str, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    low = text.lower()
    ent: dict[str, Any] = {}
    if m := re.search(r"\bTKT-?(\d+)\b", text, re.I):
        ent["ticket_number"] = f"TKT-{m.group(1)}"
    if m := re.search(r"\b(EXP-[A-Z0-9]{4,})\b", text, re.I):
        ent["claim_id"] = m.group(1).upper()
    if m := re.search(r"(?:(\$|usd|€|eur|£|gbp|₹|inr|rs\.?)\s?)(\d[\d,]*(?:\.\d{1,2})?)|(\d[\d,]*(?:\.\d{1,2})?)\s?(usd|dollars|eur|euros|gbp|pounds|inr|rupees)\b", low):
        sym = (m.group(1) or m.group(4) or "").strip(".")
        amount = (m.group(2) or m.group(3)).replace(",", "")
        ent["amount"] = float(amount)
        ent["currency"] = {"$": "USD", "usd": "USD", "dollars": "USD", "€": "EUR", "eur": "EUR", "euros": "EUR",
                           "£": "GBP", "gbp": "GBP", "pounds": "GBP", "₹": "INR", "inr": "INR", "rupees": "INR",
                           "rs": "INR"}.get(sym, "USD")
    if m := re.search(r"\b(\d{1,2})\s*(working |business )?days?\b", low):
        ent["days"] = int(m.group(1))
    elif re.search(r"\b(a|one) day\b", low):
        ent["days"] = 1
    if d := _parse_date(text, today):
        ent["date"] = d
    if m := re.search(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", low):
        hour = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
        ent["time"] = f"{hour:02d}:{m.group(2) or '00'}"
    elif m := re.search(r"\bat\s+(\d{1,2}):(\d{2})\b", low):
        ent["time"] = f"{int(m.group(1)):02d}:{m.group(2)}"
    if m := re.search(r"\b(?:for|with)\s+(\d{1,3})\s+(people|persons|attendees|of us)\b", low):
        ent["capacity"] = int(m.group(1))
    for s in SYSTEMS:
        if re.search(rf"\b{s}\b", low):
            ent["system"] = s.upper() if len(s) <= 4 else s.title()
            break
    if m := re.search(r"\b(?:install|need|request|get)\s+(?:a |an |the )?([A-Za-z][\w.+ -]{1,30}?)(?:\s+(?:software|license|licence|app|installed|on|for|please)\b|[.?!,]|$)", text, re.I):
        name = m.group(1).strip()
        if name.lower() not in {"help", "access", "admin", "to", "some", "my", "leave", "a", "new"}:
            ent["software_name"] = name
    for lt in ("sick", "casual", "parental", "unpaid", "annual"):
        if lt in low:
            ent["leave_type"] = lt
            break
    if m := re.search(r"\bfor\s+(\d{1,2})\s*(hours?|hrs?)\b", low):
        ent["duration_hours"] = int(m.group(1))
    for cat in ("travel", "meals", "lodging", "transport", "equipment", "training"):
        if cat.rstrip("s") in low:
            ent["category"] = cat
            break
    if "hotel" in low:
        ent.setdefault("category", "lodging")
    if any(w in low for w in ("taxi", "uber", "cab", "train")):
        ent.setdefault("category", "transport")
    if any(w in low for w in ("lunch", "dinner", "meal", "food")):
        ent.setdefault("category", "meals")
    return ent


def heuristic_classify(text: str) -> dict[str, Any]:
    """Returns the same shape as the LLM classifier, plus an `evidence` block for the trace."""
    scores = score_departments(text)
    tool, tool_dept = match_action(text)
    if tool_dept and tool_dept != "*":
        scores[tool_dept]["score"] += 4  # an explicit action pattern is strong evidence

    ranked = sorted(scores.items(), key=lambda kv: kv[1]["score"], reverse=True)
    (top, top_s), (second, second_s) = ranked[0], ranked[1]
    low = text.lower().strip()

    if wants_human(text):
        intent = "escalation_request"
    elif low.strip("!.? ") in GREETINGS or (len(low) < 25 and any(low.startswith(g) for g in GREETINGS) and top_s["score"] == 0):
        intent = "greeting"
    elif tool in ("get_ticket_status", "get_reimbursement_status"):
        intent = "status_inquiry"
    elif tool:
        intent = "action_request"
    elif top_s["score"] > 0 or low.endswith("?") or low.startswith(QUESTION_STARTS):
        intent = "knowledge_query"
    else:
        intent = "unclear"

    if top_s["score"] == 0:
        department, confidence = "general", 0.2 if intent not in ("greeting", "escalation_request") else 0.9
    else:
        margin = (top_s["score"] - second_s["score"]) / (top_s["score"] + 1)
        confidence = round(min(0.95, 0.45 + 0.08 * top_s["score"] + 0.3 * margin), 2)
        department = top

    secondary = [d for d, s in ranked[1:] if s["score"] >= 3 and s["score"] >= 0.6 * top_s["score"]]
    topics = sensitive_topics(text)
    risk = "high" if topics else ("medium" if tool in ("grant_admin_access", "update_bank_details", "submit_expense_claim") else "low")
    return {
        "intent": intent,
        "department": department,
        "secondary_departments": secondary,
        "urgency": urgency(text),
        "risk": risk,
        "entities": extract_entities(text),
        "required_action": tool or "none",
        "confidence": confidence,
        "rationale": f"keyword scores {', '.join(f'{d}={s['score']:.0f}' for d, s in ranked)}"
                     + (f"; action pattern -> {tool}" if tool else ""),
        "evidence": {d: s["matched"] for d, s in scores.items() if s["matched"]},
    }


def heuristic_tool_args(tool: str, text: str, entities: dict[str, Any], employee: dict) -> dict[str, Any]:
    """Fill tool arguments from extracted entities. Missing required fields are caught by validation."""
    e = entities
    if tool in ("reset_password", "unlock_account"):
        return {"system": e.get("system", "Corporate SSO")}
    if tool == "request_software":
        return {k: v for k, v in {"software_name": e.get("software_name"), "justification": text[:200]}.items() if v}
    if tool == "grant_admin_access":
        return {k: v for k, v in {"system": e.get("system"), "duration_hours": e.get("duration_hours", 8)}.items() if v}
    if tool == "submit_leave_request":
        return {k: v for k, v in {"leave_type": e.get("leave_type", "annual"), "start_date": e.get("date"),
                                  "days": e.get("days")}.items() if v}
    if tool == "submit_expense_claim":
        return {k: v for k, v in {"amount": e.get("amount"), "currency": e.get("currency", "USD"),
                                  "category": e.get("category", "other"), "description": text[:200]}.items() if v}
    if tool == "get_reimbursement_status":
        return {"claim_id": e.get("claim_id")} if e.get("claim_id") else {}
    if tool == "book_meeting_room":
        return {k: v for k, v in {"capacity": e.get("capacity", 4), "date": e.get("date"), "time": e.get("time")}.items() if v}
    if tool == "get_ticket_status":
        return {"ticket_number": e["ticket_number"]} if e.get("ticket_number") else {}
    if tool == "create_ticket":
        first = re.split(r"(?<=[.!?])\s", text.strip())[0]
        return {"title": first[:120] if len(first) >= 5 else f"Support request from {employee['name']}",
                "description": text, "priority": urgency(text)}
    return {}


# ---- Extractive answer (no-LLM generation) -------------------------------------------------------------
def extractive_answer(query_terms: list[str], sources: list[dict[str, Any]], max_sentences: int = 4) -> str:
    terms = set(query_terms)
    scored: list[tuple[float, int, str]] = []
    for idx, src in enumerate(sources, 1):
        for raw in re.split(r"(?<=[.!?])\s+|\n+", src["content"]):
            s = raw.strip(" -*#\t")
            if len(s) < 25:
                continue
            words = set(re.findall(r"[a-z0-9]+", s.lower()))
            overlap = len(terms & words)
            if overlap:
                scored.append((overlap + 0.5 / idx, idx, s))
    if not scored:
        return ""
    best = sorted(scored, key=lambda x: x[0], reverse=True)[:max_sentences]
    seen, lines = set(), []
    for _, idx, s in best:
        if s not in seen:
            seen.add(s)
            lines.append(f"- {s.rstrip('.')}. [{idx}]")
    return "Here's what our policy says:\n\n" + "\n".join(lines)
