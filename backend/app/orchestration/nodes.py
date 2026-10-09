"""Workflow nodes. Each node is a small, typed step; routing decisions live in graph.py.

Nodes return partial state updates. A `_summary` key is a one-line, human-readable description of what the
step did; tracing pops it and sends it to the workflow pane.
"""

import asyncio
import hashlib
import json
import re
from datetime import date
from typing import Any, Literal

from langgraph.types import interrupt
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from app.agents.registry import get_agent, load_agents, load_prompt
from app.agents.tools import TOOLS, ToolContext, ToolDenied
from app.config import settings
from app.core.metrics import ESCALATIONS, OUTCOMES, RETRIEVAL_TOP_SCORE, ROUTING, TOOL_CALLS
from app.db.models import Attachment, Conversation, Employee, Message, ToolInvocation
from app.db.session import session_scope
from app.llm import client as llm
from app.orchestration import heuristics as h
from app.orchestration.state import SupportState
from app.orchestration.tracing import emit, traced
from app.retrieval.search import hybrid_search, query_terms, rerank
from app.services import tickets as ticket_svc
from app.services.audit import audit

SENSITIVE_QUEUE = {
    "harassment": "HR Employee Relations (Confidential)",
    "safety": "Employee Wellbeing & Security (Urgent)",
    "legal": "Legal & Compliance",
    "fraud": "Ethics & Compliance",
    "security_incident": "Security Operations Center",
    "termination": "HR Employee Relations (Confidential)",
}


def _fill(template: str, **values: Any) -> str:
    for k, v in values.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def _history_for_llm(state: SupportState, limit: int = 6) -> list[dict[str, str]]:
    msgs = []
    for m in state.get("history", [])[-limit:]:
        redacted, _ = h.redact_pii(m["content"])
        msgs.append({"role": m["role"], "content": redacted[:1500]})
    return msgs


def _user_content(state: SupportState) -> str:
    text = state.get("redacted_message") or state["message"]
    for a in state.get("attachments", []):
        if a.get("text"):
            snippet, _ = h.redact_pii(a["text"][:1500])
            text += f"\n\n[Attachment: {a['filename']}]\n{snippet}"
    return text


def normalize_citations(text: str) -> str:
    """Models cite in assorted styles (【1】, [1†L3], [1, 2]); normalise to [1][2] for validation and the UI."""
    text = re.sub(r"【(\d+)[^】]*】", r"[\1]", text)
    text = re.sub(r"\[(\d+)†[^\]]*\]", r"[\1]", text)
    return re.sub(r"\[(\d+(?:\s*,\s*\d+)+)\]", lambda m: "".join(f"[{n.strip()}]" for n in m.group(1).split(",")), text)


def _jsonable(v: Any) -> Any:
    if isinstance(v, float | int | str | bool) or v is None:
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)


# =========================================================================================================
# Orchestrator
# =========================================================================================================
@traced("intake")
async def intake(state: SupportState) -> dict:
    ids = [a["id"] for a in state.get("attachments", [])]
    async with session_scope() as db:
        convo = await db.get(Conversation, state["conversation_id"])
        rows = (await db.execute(
            select(Message).where(Message.conversation_id == state["conversation_id"])
            .order_by(Message.created_at.desc()).limit(9)
        )).scalars().all()
        attachments = []
        if ids:
            found = (await db.execute(
                select(Attachment).where(Attachment.id.in_(ids), Attachment.employee_id == state["employee"]["id"])
            )).scalars().all()
            attachments = [{"id": a.id, "filename": a.filename, "content_type": a.content_type,
                            "scan_status": a.scan_status, "extraction_status": a.extraction_status,
                            "text": (a.extracted_text or "")[:4000]} for a in found]
    # rows[0] is the message being processed (already persisted by the API); history excludes it.
    history = [{"role": m.role, "content": m.content} for m in reversed(rows[1:])]
    pending = (convo.context or {}).get("pending_plan") if convo else None
    return {
        "attachments": attachments,
        "history": history,
        "pending_plan": pending,
        "_summary": f"{len(state['message'])} chars, {len(attachments)} attachment(s), {len(history)} prior message(s)"
                    + (f", resuming pending '{pending['tool']}' plan" if pending else ""),
    }


@traced("authenticate")
async def authenticate(state: SupportState) -> dict:
    issues: list[str] = []
    async with session_scope() as db:
        emp = await db.get(Employee, state["employee"]["id"])
        if emp is None or not emp.is_active:
            issues.append("employee is not active")
    msg = state["message"].strip()
    if not msg:
        issues.append("empty message")
    if len(msg) > settings.max_message_chars:
        issues.append(f"message exceeds {settings.max_message_chars} characters")
    for a in state.get("attachments", []):
        if a["scan_status"] != "clean":
            issues.append(f"attachment {a['filename']} failed security scan ({a['scan_status']})")
    validation = {"ok": not issues, "issues": issues, "employee": state["employee"]["id"],
                  "role": state["employee"]["role"], "department": state["employee"]["department"],
                  "country": state["employee"]["country"]}
    update: dict[str, Any] = {"validation": validation}
    if issues:
        update.update(outcome="rejected", final_response="I couldn't process that message: " + "; ".join(issues) + ".")
    update["_summary"] = "identity and input OK" if not issues else "; ".join(issues)
    return update


@traced("guardrails")
async def guardrails(state: SupportState) -> dict:
    text = state["message"]
    injection = h.detect_injection(text)
    redacted, pii = h.redact_pii(text)
    for a in state.get("attachments", []):
        _, apii = h.redact_pii(a.get("text", ""))
        pii += [f"attachment:{p}" for p in apii]
    topics = h.sensitive_topics(text)
    g = {"blocked": bool(injection), "prompt_injection": injection, "pii_redacted": pii, "sensitive_topics": topics}
    update: dict[str, Any] = {"guardrail": g, "redacted_message": redacted}
    if injection:
        update.update(outcome="rejected", final_response=(
            "I can't act on instructions that try to change how I operate. "
            "Please describe the support issue you need help with."))
    parts = []
    if injection:
        parts.append("prompt injection blocked")
    if pii:
        parts.append(f"redacted {', '.join(pii)}")
    if topics:
        parts.append(f"sensitive: {', '.join(topics)}")
    update["_summary"] = "; ".join(parts) or "no issues"
    return update


class ClassificationOut(BaseModel):
    intent: Literal["knowledge_query", "action_request", "status_inquiry", "escalation_request", "greeting", "unclear"]
    department: str
    secondary_departments: list[str] = Field(default_factory=list)
    urgency: Literal["P1", "P2", "P3", "P4"] = "P3"
    risk: Literal["low", "medium", "high"] = "low"
    entities: dict[str, Any] = Field(default_factory=dict)
    required_action: str = "none"
    confidence: float = Field(ge=0, le=1)
    rationale: str = ""


@traced("classify")
async def classify(state: SupportState) -> dict:
    agents = load_agents()
    message = state.get("redacted_message") or state["message"]
    heur = h.heuristic_classify(message)
    pending = state.get("pending_plan")

    # Follow-up turn that supplies fields a previous tool plan was missing.
    if pending and heur["intent"] not in ("greeting", "escalation_request") and not h.sensitive_topics(message):
        new_ents = h.extract_entities(message)
        if new_ents or len(message) < 80:
            c = {**heur, "intent": "action_request", "department": pending["agent"], "confidence": 0.85,
                 "required_action": pending["tool"], "entities": {**pending.get("entities", {}), **new_ents},
                 "rationale": f"continuing pending {pending['tool']} plan with newly supplied details", "source": "pending"}
            return {"classification": c, "_summary": f"follow-up for {pending['tool']} ({pending['agent']})"}

    source, usage = "heuristic", []
    c = heur
    if settings.llm_enabled:
        dept_lines = "\n".join(f"- {a.id}: {a.label} — {a.description}" for a in agents.values())
        dept_lines += "\n- general: none of the above / cannot tell"
        try:
            out, res = await llm.complete_json(
                system=_fill(load_prompt("classify"), departments=dept_lines),
                messages=[*_history_for_llm(state), {"role": "user", "content": _user_content(state)}],
                schema=ClassificationOut, purpose="classify",
            )
            c = out.model_dump()
            if c["department"] not in agents:
                c["department"] = "general"
            c["secondary_departments"] = [d for d in c["secondary_departments"] if d in agents and d != c["department"]]
            # Deterministic extractors are better at dates/amounts; LLM values take precedence when present.
            c["entities"] = {**heur["entities"], **{k: v for k, v in c["entities"].items() if v not in (None, "")}}
            if heur["risk"] == "high":
                c["risk"] = "high"  # keyword safety net never gets overruled
            source, usage = f"llm:{res.model}", [res.usage("classify")]
        except llm.LLMUnavailable as exc:
            source = "heuristic (llm unavailable)"
            c = {**heur, "llm_error": str(exc)[:300]}
    c["source"] = source
    return {
        "classification": c,
        "llm_usage": usage,
        "_summary": f"{c['intent']} → {c['department']} ({c['confidence']:.0%}), {c['urgency']}, risk {c['risk']}"
                    + (f", action {c['required_action']}" if c.get("required_action") not in (None, "none") else "")
                    + f" [{source}]",
    }


@traced("route")
async def route(state: SupportState) -> dict:
    c = state.get("classification", {})
    g = state.get("guardrail", {})
    if state.get("errors"):
        decision, reason = "escalate", "classification failed"
    elif c.get("intent") == "escalation_request":
        decision, reason = "escalate", "employee asked for a human"
    elif c.get("risk") == "high" or g.get("sensitive_topics"):
        decision, reason = "escalate", f"sensitive topic ({', '.join(g.get('sensitive_topics') or ['high risk'])})"
    elif c.get("intent") == "greeting":
        decision, reason = "smalltalk", "greeting / small talk"
    elif c.get("department") not in load_agents() or c.get("intent") == "unclear" \
            or c.get("confidence", 0) < settings.clarify_threshold:
        decision, reason = "clarify", f"ambiguous (confidence {c.get('confidence', 0):.0%})"
    else:
        decision, reason = "agent", f"confidence {c['confidence']:.0%} ≥ {settings.clarify_threshold:.0%}"
    agent = c.get("department") if decision == "agent" else None
    ROUTING.labels(department=c.get("department", "none"), route=decision).inc()
    target = f"{get_agent(agent).label}" if agent else decision
    return {"route": decision, "agent": agent, "_summary": f"→ {target}: {reason}"}


@traced("clarify")
async def clarify(state: SupportState) -> dict:
    c = state.get("classification", {})
    options = "\n".join(f"- **{a.label.replace(' Agent', '')}** — {a.description}" for a in load_agents().values())
    text = (
        "I want to make sure I get this to the right place. Which of these is it about?\n\n"
        f"{options}\n\nA little more detail — the system or policy involved, what you've tried, "
        "and any error message — helps me resolve it right away."
    )
    if c.get("secondary_departments"):
        text = "It sounds like this touches more than one team. " + text
    return {"final_response": text, "outcome": "clarification", "_summary": "asked the employee to clarify"}


# =========================================================================================================
# Department agent (subgraph): plan → knowledge | action
# =========================================================================================================
@traced("plan")
async def plan(state: SupportState) -> dict:
    agent = get_agent(state["agent"])
    c = state["classification"]
    pending = state.get("pending_plan")
    msg = state.get("redacted_message") or state["message"]

    if pending and c.get("source") == "pending":
        args = {**pending.get("args", {}),
                **h.heuristic_tool_args(pending["tool"], msg, c.get("entities", {}), state["employee"])}
        args = {k: v for k, v in args.items() if k in TOOLS[pending["tool"]].args_model.model_fields}
        p = {"mode": "action", "tool": pending["tool"], "args": args, "source": "pending plan + new details"}
        return {"plan": p, "_summary": f"resume {p['tool']} with {sorted(args)}"}

    wants_action = c.get("intent") in ("action_request", "status_inquiry")
    if not wants_action:
        return {"plan": {"mode": "knowledge", "source": "intent"}, "_summary": "knowledge query → RAG"}

    llm_usage: list[dict] = []
    if settings.llm_enabled:
        e = state["employee"]
        system = _fill(
            load_prompt("plan"), agent_label=agent.label, agent_description=agent.description,
            today=date.today().isoformat(), employee_name=e["name"], employee_role=e["role"],
            employee_department=e["department"], employee_country=e["country"], agent_instructions=agent.instructions,
        )
        try:
            tool, args, _text, res = await llm.choose_tool(
                system=system, messages=[*_history_for_llm(state), {"role": "user", "content": _user_content(state)}],
                tools=[TOOLS[t].openai_schema() for t in agent.tools if t in TOOLS], purpose="plan",
            )
            if tool:
                p = {"mode": "action", "tool": tool, "args": args, "source": f"llm:{res.model}"}
                return {"plan": p, "llm_usage": [res.usage("plan")], "_summary": f"tool {tool} {json.dumps(args)[:160]}"}
            # The model declined to call a tool (often because details are missing); the classifier still
            # says this is an action, so fall back to the deterministic planner and let validation ask.
            llm_usage = [res.usage("plan")]
        except llm.LLMUnavailable:
            pass  # fall through to the deterministic planner

    tool = c.get("required_action")
    if tool not in agent.tools:
        tool = h.match_action(msg)[0]
    if tool not in agent.tools:
        tool = "create_ticket" if c.get("intent") == "action_request" and "create_ticket" in agent.tools else None
    if not tool:
        return {"plan": {"mode": "knowledge", "source": "heuristic"}, "llm_usage": llm_usage,
                "_summary": "no matching tool → RAG"}
    args = h.heuristic_tool_args(tool, state["message"], c.get("entities", {}), state["employee"])
    p = {"mode": "action", "tool": tool, "args": args, "source": "heuristic"}
    return {"plan": p, "llm_usage": llm_usage, "_summary": f"tool {tool} {json.dumps(args)[:160]} [heuristic]"}


@traced("retrieve")
async def retrieve(state: SupportState) -> dict:
    agent = get_agent(state["agent"])
    query = state.get("redacted_message") or state["message"]
    if len(query) < 40 and state.get("history"):  # short follow-ups borrow context from the previous user turn
        prev = next((m["content"] for m in reversed(state["history"]) if m["role"] == "user"), "")
        query = f"{prev} {query}"
    for a in state.get("attachments", []):
        if a.get("text"):
            query += " " + a["text"][:300]
    async with session_scope() as db:
        hits, hidden = await hybrid_search(db, query, state["employee"], agent.knowledge_collections,
                                           settings.retrieval_top_k)
    retrieved = [{k: _jsonable(v) for k, v in hit.items()} for hit in hits]
    both = sum(1 for r in retrieved if r.get("vec_rank") and r.get("fts_rank"))
    return {
        "retrieved": retrieved,
        "_summary": f"{len(retrieved)} candidates from {agent.knowledge_collections} "
                    f"({both} matched by both vector and keyword)"
                    + (f"; {hidden} document(s) hidden by access policy" if hidden else ""),
    }


@traced("rerank")
async def rerank_node(state: SupportState) -> dict:
    query = state.get("redacted_message") or state["message"]
    ranked = rerank(query, list(state.get("retrieved", [])), settings.rerank_keep)
    kept = [r for r in ranked if r["score"] >= settings.min_rerank_score]
    sources = [{
        "n": i, "chunk_id": r["chunk_id"], "document_id": r["document_id"], "title": r["title"],
        "heading": r.get("heading"), "source": r["source"], "version": r["version"], "content": r["content"],
        "score": r["score"], "coverage": r["coverage"],
    } for i, r in enumerate(kept, 1)]
    if sources:
        RETRIEVAL_TOP_SCORE.observe(sources[0]["score"])
    return {
        "sources": sources,
        "_summary": f"kept {len(sources)}/{len(ranked)}"
                    + (f"; top '{sources[0]['title']} › {sources[0]['heading']}' score {sources[0]['score']}" if sources else
                       f"; nothing above {settings.min_rerank_score}"),
    }


@traced("generate")
async def generate(state: SupportState) -> dict:
    sources = state.get("sources", [])
    if not sources:
        return {"answer": "", "citations": [], "_summary": "no relevant sources — nothing to ground an answer on"}
    agent = get_agent(state["agent"])
    query = state.get("redacted_message") or state["message"]
    usage: list[dict] = []
    answer = ""
    source_label = "extractive"

    if settings.llm_enabled:
        src_block = "\n\n".join(
            f"[{s['n']}] {s['title']} › {s.get('heading') or 'Overview'} (v{s['version']})\n{s['content']}" for s in sources
        )
        e = state["employee"]
        system = _fill(load_prompt("generate"), agent_label=agent.label, employee_name=e["name"],
                       employee_country=e["country"], agent_instructions=agent.instructions, sources=src_block)
        try:
            answer, res = await llm.stream_text(
                system=system,
                messages=[*_history_for_llm(state, 4), {"role": "user", "content": _user_content(state)}],
                on_token=lambda t: emit({"type": "token", "text": t}),
                purpose="generate",
            )
            usage, source_label = [res.usage("generate")], f"llm:{res.model}"
        except llm.LLMUnavailable:
            answer = ""

    if not answer:
        answer = h.extractive_answer(query_terms(query), sources)
        for i in range(0, len(answer), 24):  # stream the extractive answer too, so the UX is identical
            emit({"type": "token", "text": answer[i:i + 24]})
            await asyncio.sleep(0.01)

    answer = normalize_citations(answer)
    cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer)})
    by_n = {s["n"]: s for s in sources}
    citations = [{"n": n, "title": by_n[n]["title"], "heading": by_n[n].get("heading"), "source": by_n[n]["source"],
                  "version": by_n[n]["version"], "document_id": by_n[n]["document_id"],
                  "snippet": by_n[n]["content"][:280]} for n in cited if n in by_n]
    return {"answer": answer, "citations": citations, "llm_usage": usage,
            "_summary": f"{len(answer)} chars, cites {[c['n'] for c in citations]} [{source_label}]"}


@traced("validate_action")
async def validate_action(state: SupportState) -> dict:
    agent = get_agent(state["agent"])
    p = state["plan"]
    tool = p.get("tool")
    if tool not in TOOLS or tool not in agent.tools:
        v = {"ok": False, "error": f"tool '{tool}' is not approved for {agent.label}"}
        return {"tool_validation": v, "outcome": "rejected",
                "final_response": "That isn't something I'm permitted to do automatically. I can open a ticket for a specialist instead.",
                "_summary": v["error"]}
    spec = TOOLS[tool]
    try:
        model = spec.args_model.model_validate(p.get("args", {}))
    except ValidationError as exc:
        missing, invalid = [], []
        for err in exc.errors():
            field = str(err["loc"][0]) if err["loc"] else "input"
            (missing if err["type"] == "missing" else invalid).append((field, err["msg"]))
        fields = spec.args_model.model_fields
        lines = [f"- **{f.replace('_', ' ')}**: {fields[f].description or ''}".rstrip(": ") for f, _ in missing if f in fields]
        lines += [f"- **{f.replace('_', ' ')}**: {m}" for f, m in invalid]
        text = f"I can {spec.description[0].lower() + spec.description[1:].rstrip('.')} — I just need a bit more:\n\n" + "\n".join(lines)
        pending = {"agent": agent.id, "tool": tool,
                   "args": {k: v for k, v in p.get("args", {}).items() if k not in {f for f, _ in invalid}},
                   "entities": state["classification"].get("entities", {})}
        v = {"ok": False, "missing": [f for f, _ in missing], "invalid": [f"{f}: {m}" for f, m in invalid]}
        return {"tool_validation": v, "outcome": "clarification", "final_response": text, "pending_plan": pending,
                "_summary": f"needs {v['missing'] + v['invalid']}"}
    v = {"ok": True, "tool": tool, "args": model.model_dump(mode="json")}
    return {"tool_validation": v, "pending_plan": None, "_summary": f"{tool} args valid: {json.dumps(v['args'])[:160]}"}


@traced("authorize")
async def authorize(state: SupportState) -> dict:
    v = state["tool_validation"]
    spec = TOOLS[v["tool"]]
    e = state["employee"]
    args = spec.args_model.model_validate(v["args"])
    if spec.allowed_roles and e["role"] not in spec.allowed_roles:
        a = {"allowed": False, "reason": f"role '{e['role']}' may not call {spec.name}"}
        return {"authorization": a, "outcome": "rejected",
                "final_response": "You don't have permission for that action. I can raise a ticket so the right team can help.",
                "_summary": a["reason"]}
    needs = spec.sensitive(args)
    approver_name = None
    async with session_scope() as db:
        if spec.approver == "manager" and e.get("manager_id"):
            mgr = await db.get(Employee, e["manager_id"])
            approver_name = mgr.name if mgr else None
        elif spec.approver != "manager":
            row = (await db.execute(select(Employee).where(Employee.role == spec.approver).limit(1))).scalar_one_or_none()
            approver_name = row.name if row else None
    a = {"allowed": True, "requires_approval": needs, "approver_role": spec.approver, "approver_name": approver_name,
         "reason": spec.sensitivity_reason if needs else "within self-service policy",
         "scope": "acts only on the requesting employee's own records", "irreversible": spec.irreversible}
    return {"authorization": a,
            "_summary": f"allowed; {'needs ' + spec.approver + ' approval: ' + spec.sensitivity_reason if needs else 'no approval needed'}"}


@traced("approval", timeout=False)
async def approval(state: SupportState) -> dict:
    v, a = state["tool_validation"], state["authorization"]
    decision = interrupt({
        "kind": "approval_required",
        "tool": v["tool"],
        "description": TOOLS[v["tool"]].description,
        "args": v["args"],
        "reason": a["reason"],
        "approver_role": a["approver_role"],
        "approver_name": a.get("approver_name"),
        "requester": state["employee"]["name"],
    })
    approved = bool(decision.get("approved"))
    result = {"approved": approved, "approver": decision.get("approver"), "comment": decision.get("comment", "")}
    update: dict[str, Any] = {"approval": result,
                              "_summary": f"{'approved' if approved else 'declined'} by {decision.get('approver')}"}
    if not approved:
        update.update(outcome="rejected", final_response=(
            f"Your request ({v['tool'].replace('_', ' ')}) was declined by the approver"
            + (f": “{result['comment']}”" if result["comment"] else ".")))
    return update


@traced("execute")
async def execute(state: SupportState) -> dict:
    v = state["tool_validation"]
    spec = TOOLS[v["tool"]]
    agent = get_agent(state["agent"])
    canonical = json.dumps(v["args"], sort_keys=True)
    key = hashlib.sha256(f"{state['execution_id']}|{spec.name}|{canonical}".encode()).hexdigest()

    async with session_scope() as db:
        prior = (await db.execute(select(ToolInvocation).where(ToolInvocation.idempotency_key == key))).scalar_one_or_none()
        if prior and prior.status == "succeeded":
            TOOL_CALLS.labels(tool=spec.name, outcome="replayed").inc()
            return {"tool_result": {"status": "succeeded", "tool": spec.name, "result": prior.result, "replayed": True},
                    "_summary": f"idempotent replay of {spec.name}"}
        ctx = ToolContext(db=db, employee=state["employee"], agent_id=agent.id, queue=agent.support_queue,
                          conversation_id=state["conversation_id"], message=state["message"])
        try:
            result = await spec.handler(ctx, spec.args_model.model_validate(v["args"]))
            status, error = "succeeded", None
        except ToolDenied as exc:
            result, status, error = None, "denied", str(exc)
        except Exception as exc:  # downstream failure → escalate in verify
            result, status, error = None, "failed", f"{type(exc).__name__}: {exc}"
        if prior is None:
            db.add(ToolInvocation(execution_id=state["execution_id"], employee_id=state["employee"]["id"], tool=spec.name,
                                  args=v["args"], idempotency_key=key, status=status, result=result, error=error))
        audit(db, state["employee"]["id"], f"tool.{status}", "tool", spec.name,
              correlation_id=state.get("correlation_id"), args=v["args"], error=error)

    TOOL_CALLS.labels(tool=spec.name, outcome=status).inc()
    tr = {"status": status, "tool": spec.name, "result": result, "error": error, "idempotency_key": key[:16]}
    update: dict[str, Any] = {"tool_result": tr, "_summary": f"{spec.name} → {status}" + (f": {error}" if error else "")}
    if status == "denied":
        update.update(outcome="rejected", final_response=error)
    return update


# =========================================================================================================
# Verification, escalation and response
# =========================================================================================================
@traced("verify")
async def verify(state: SupportState) -> dict:
    c = state.get("classification", {})
    outcome = state.get("outcome")
    reasons: list[str] = []
    escalate, confidence = False, c.get("confidence", 0.0)

    if state.get("errors"):
        escalate, confidence = True, 0.0
        reasons.append("system error in " + ", ".join(e["node"] for e in state["errors"]))
    elif outcome in ("clarification", "rejected"):
        reasons.append(f"no verification needed ({outcome})")
    elif state.get("plan", {}).get("mode") == "action":
        tr = state.get("tool_result") or {}
        if tr.get("status") == "succeeded":
            confidence = 0.92
            reasons.append(f"{tr['tool']} succeeded")
        else:
            escalate, confidence = True, 0.1
            reasons.append(f"tool {tr.get('tool')} {tr.get('status', 'did not run')}")
    else:
        sources, answer = state.get("sources", []), state.get("answer", "")
        if not sources or not answer:
            escalate, confidence = True, 0.1
            reasons.append("no relevant knowledge found")
        else:
            sentences = [s for s in re.split(r"(?<=[.!?])\s+|\n+", answer) if len(s.strip()) > 20]
            cited = [s for s in sentences if re.search(r"\[\d+\]", s)]
            valid_refs = {s["n"] for s in sources}
            refs = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
            invalid = refs - valid_refs
            citation_ratio = (len(cited) / len(sentences)) if sentences else 0.0
            if invalid:
                citation_ratio *= 0.5
                reasons.append(f"invalid citations {sorted(invalid)}")
            top = sources[0]["score"]
            confidence = round(0.35 * c.get("confidence", 0.5) + 0.35 * min(1.0, top / 0.7) + 0.30 * citation_ratio, 3)
            reasons.append(f"retrieval {top:.2f}, citation coverage {citation_ratio:.0%}, routing {c.get('confidence', 0):.0%}")
            if not refs - invalid:
                escalate = True
                reasons.append("answer has no valid citations (ungrounded)")
            elif confidence < settings.confidence_threshold:
                escalate = True
                reasons.append(f"confidence {confidence:.0%} below threshold {settings.confidence_threshold:.0%}")

    ver = {"resolved": not escalate and outcome not in ("clarification", "rejected"), "confidence": confidence,
           "escalate": escalate, "reasons": reasons, "threshold": settings.confidence_threshold}
    return {"verification": ver,
            "_summary": f"confidence {confidence:.0%} → {'escalate' if escalate else 'ok'} ({'; '.join(reasons)})"}


def _transcript(state: SupportState) -> str:
    lines = [f"{m['role']}: {m['content'][:500]}" for m in state.get("history", [])[-8:]]
    lines.append(f"user: {state.get('redacted_message') or state['message']}")
    return "\n".join(lines)


@traced("escalate")
async def escalate(state: SupportState) -> dict:
    c = state.get("classification", {})
    g = state.get("guardrail", {})
    agents = load_agents()
    dept = state.get("agent") or (c.get("department") if c.get("department") in agents else None)
    topics = g.get("sensitive_topics") or []
    if topics:
        queue, reason, priority = SENSITIVE_QUEUE.get(topics[0], "HR Employee Relations (Confidential)"), f"sensitive:{topics[0]}", "P2"
        dept = dept or ("it" if topics[0] == "security_incident" else "hr")
    elif c.get("intent") == "escalation_request":
        reason, priority = "employee_requested_human", c.get("urgency", "P3")
        queue = agents[dept].support_queue if dept else "Employee Support Center"
    else:
        v = state.get("verification", {})
        reason = "tool_failure" if state.get("tool_result", {}).get("status") == "failed" else (
            "system_error" if state.get("errors") else "low_confidence")
        priority = c.get("urgency", "P3")
        queue = agents[dept].support_queue if dept else "Employee Support Center"
        reason += f" ({v.get('confidence', 0):.0%})" if reason == "low_confidence" else ""
    dept = dept or "general"
    ESCALATIONS.labels(reason=reason.split(" ")[0].split(":")[0]).inc()

    async with session_scope() as db:
        t = await ticket_svc.conversation_ticket(db, state["conversation_id"])
        description = f"Escalated by support assistant.\nReason: {reason}\n\nConversation:\n{_transcript(state)}"
        if state.get("answer"):
            description += f"\n\nAssistant's draft answer:\n{state['answer']}"
        if t and t.status not in ("resolved", "closed"):
            t = await ticket_svc.update_ticket(db, t, state["employee"]["id"], status="escalated", assigned_queue=queue,
                                               escalation_reason=reason, priority=priority, description=description)
        else:
            t = await ticket_svc.create_ticket(
                db, employee_id=state["employee"]["id"], department=dept, category="escalation",
                title=(state.get("redacted_message") or state["message"])[:120], description=description,
                priority=priority, queue=queue, conversation_id=state["conversation_id"], status="escalated",
                escalation_reason=reason,
            )
        audit(db, "agent", "conversation.escalated", "ticket", t.display_id,
              correlation_id=state.get("correlation_id"), reason=reason, queue=queue)
        ticket = ticket_svc.ticket_dict(t)

    if topics:
        text = ("Thank you for telling me. This needs a person, not an automated assistant, so I've passed it "
                f"confidentially to **{queue}** ({ticket['number']}). Only that team can see it. ")
        if "safety" in topics:
            text += "If you are in immediate danger, please contact local emergency services or the 24/7 Employee Assistance Program now."
        else:
            text += "Someone will reach out to you directly, usually within one business day."
    else:
        prefix = (state.get("answer") + "\n\n---\n\n") if state.get("answer") else ""
        why = {"employee_requested_human": "as you asked",
               "tool_failure": "because the automated action didn't complete",
               "system_error": "because something went wrong on my side"}.get(reason.split(" ")[0],
               "because I'm not confident enough in my answer to close this on my own")
        text = (f"{prefix}I've escalated this to **{queue}** {why} — ticket **{ticket['number']}** "
                f"({priority}). They'll see this full conversation, so you won't need to repeat yourself.")
    return {"escalation": {"reason": reason, "queue": queue, "priority": priority}, "ticket": ticket,
            "final_response": text, "outcome": "escalated", "_summary": f"{reason} → {queue} ({ticket['number']})"}


def _format_tool_result(tool: str, r: dict) -> str:
    fmt = {
        "reset_password": lambda: f"Done — {r['delivery']} (ref {r['reset_reference']})",
        "unlock_account": lambda: f"Your {r['system']} account is unlocked. {r['note']}",
        "request_software": lambda: f"Software request **{r['request_id']}** for {r['software']} is in. {r['eta']}",
        "grant_admin_access": lambda: f"Admin access to {r['system']} granted for {r['expires_in_hours']}h (grant {r['grant_id']}). {r['note']}",
        "get_leave_balance": lambda: f"Your leave balance as of {r['as_of']}: **{r['annual']}** annual, **{r['sick']}** sick, **{r['casual']}** casual days.",
        "submit_leave_request": lambda: f"Leave request **{r['leave_request_id']}** ({r['leave_type']}, {r['start_date']} → {r['end_date']}) was submitted to your manager.",
        "update_bank_details": lambda: f"{r['note']} (case {r['case_id']})",
        "submit_expense_claim": lambda: f"Expense claim **{r['claim_id']}** for {r['amount']:.2f} {r['currency']} ({r['category']}) was submitted. Expected payout: {r['expected_payout']}.",
        "get_reimbursement_status": lambda: f"Claim **{r['claim_id']}** ({r['amount']:.2f}): {r['status']}.",
        "book_meeting_room": lambda: f"Booked **{r['room']}** for {r['capacity']} on {r['date']} at {r['time']} (booking {r['booking_id']}).",
        "create_ticket": lambda: f"I've opened ticket **{r['ticket']['number']}** with {r['ticket']['assigned_queue']} ({r['ticket']['priority']}).",
        "get_ticket_status": lambda: (
            f"Ticket **{r['ticket']['number']}** — {r['ticket']['title']}: status **{r['ticket']['status']}**, queue {r['ticket']['assigned_queue']}, priority {r['ticket']['priority']}."
            if r.get("found") else f"I couldn't find ticket {r['ticket_number']}."),
    }
    try:
        return fmt[tool]()
    except Exception:
        return "Done. " + ", ".join(f"{k}: {v}" for k, v in r.items())


@traced("respond")
async def respond(state: SupportState) -> dict:
    outcome = state.get("outcome")
    agent_id = state.get("agent")
    agents = load_agents()
    ticket = state.get("ticket")
    text = state.get("final_response", "")
    secondary: list[dict] = []
    e = state["employee"]

    async with session_scope() as db:
        if state.get("route") == "smalltalk":
            outcome = "smalltalk"
            text = (f"Hi {e['name'].split()[0]}! I can answer IT, HR, Finance and Facilities questions or get things done — "
                    "for example *“reset my VPN password”*, *“how many leave days do I have?”* or "
                    "*“submit a $45 taxi expense for yesterday's client visit”*.")
        elif outcome not in ("escalated", "clarification", "rejected"):
            agent = agents[agent_id]
            existing = await ticket_svc.conversation_ticket(db, state["conversation_id"], agent_id)
            if state.get("plan", {}).get("mode") == "action":
                tr = state["tool_result"]
                outcome, text = "action_executed", _format_tool_result(tr["tool"], tr["result"] or {})
                if tr["tool"] == "create_ticket":
                    ticket = tr["result"]["ticket"]
                elif tr["tool"] != "get_ticket_status":
                    resolution = f"{tr['tool']} executed by {agent.label}: {text}"
                    if existing and existing.status not in ("resolved", "closed"):
                        t = await ticket_svc.update_ticket(db, existing, e["id"], status="resolved", resolution=resolution)
                    else:
                        t = await ticket_svc.create_ticket(
                            db, employee_id=e["id"], department=agent_id, category=tr["tool"],
                            title=state["message"][:120], description=state["message"],
                            priority=state["classification"].get("urgency", "P3"), queue=agent.support_queue,
                            conversation_id=state["conversation_id"], status="resolved",
                            resolution=resolution)
                    ticket = ticket_svc.ticket_dict(t)
            else:
                outcome, text = "answered", state.get("answer", "")
                resolution = f"Answered by {agent.label} from {len(state.get('citations', []))} cited source(s)"
                if existing and existing.status not in ("resolved", "closed"):
                    t = await ticket_svc.update_ticket(db, existing, e["id"], status="resolved", resolution=resolution)
                else:
                    t = await ticket_svc.create_ticket(
                        db, employee_id=e["id"], department=agent_id, category="inquiry", title=state["message"][:120],
                        description=state["message"], priority=state["classification"].get("urgency", "P4"),
                        queue=agent.support_queue, conversation_id=state["conversation_id"], status="resolved",
                        resolution=resolution)
                ticket = ticket_svc.ticket_dict(t)
        elif outcome == "rejected" and state.get("approval") and not state["approval"].get("approved"):
            existing = await ticket_svc.conversation_ticket(db, state["conversation_id"], agent_id)
            if existing:
                t = await ticket_svc.update_ticket(db, existing, e["id"], status="closed",
                                                   resolution="Approval declined: " + state["approval"].get("comment", ""))
                ticket = ticket_svc.ticket_dict(t)

        # Multi-domain requests: open a follow-up ticket for each secondary department.
        if outcome in ("answered", "action_executed", "escalated"):
            for d in state.get("classification", {}).get("secondary_departments", []):
                if d in agents and d != agent_id:
                    t2 = await ticket_svc.create_ticket(
                        db, employee_id=e["id"], department=d, category="follow_up",
                        title=f"Follow-up: {state['message'][:100]}", description=_transcript(state),
                        priority=state["classification"].get("urgency", "P3"), queue=agents[d].support_queue,
                        conversation_id=state["conversation_id"])
                    secondary.append(ticket_svc.ticket_dict(t2))
            if secondary:
                text += "\n\nThis also involves " + ", ".join(
                    f"{agents[t['department']].label.replace(' Agent', '')} (**{t['number']}**)" for t in secondary
                ) + " — I've opened a follow-up so that part doesn't get lost."

        convo = await db.get(Conversation, state["conversation_id"])
        if convo:
            ctx = dict(convo.context or {})
            if state.get("pending_plan") and outcome == "clarification":
                ctx["pending_plan"] = state["pending_plan"]
            else:
                ctx.pop("pending_plan", None)
            convo.context = ctx
            if outcome == "escalated":
                convo.status = "escalated"
            elif outcome in ("answered", "action_executed"):
                convo.status = "active"
        audit(db, "agent", f"conversation.{outcome}", "conversation", state["conversation_id"],
              correlation_id=state.get("correlation_id"), agent=agent_id,
              confidence=state.get("verification", {}).get("confidence"))

    OUTCOMES.labels(outcome=outcome or "unknown").inc()
    return {"final_response": text, "outcome": outcome, "ticket": ticket, "secondary_tickets": secondary,
            "_summary": f"{outcome}" + (f"; ticket {ticket['number']} {ticket['status']}" if ticket else "")
                        + (f"; +{len(secondary)} follow-up ticket(s)" if secondary else "")}
