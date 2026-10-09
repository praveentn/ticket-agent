# Agentic Support Desk

An agentic RAG support platform for employee IT / HR / Finance / Facilities requests, built to the spec in
[ask.md](ask.md). A **LangGraph** orchestrator classifies each request and routes it to a department-agent
subgraph. That subgraph either answers from access-controlled knowledge, with citations, or runs an approved
tool behind validation, authorization and human approval. It then verifies the result and either resolves the
ticket or escalates it to a human with full context.

The UI's **left pane shows the workflow live**. Each LangGraph node lights up as it runs, with its duration, a
one-line summary and the exact state update it wrote, so you can see and learn how the agent reasons step by step.

## Quick start

```bash
cp .env.example .env          # add GROQ_API_KEY (optional — see below)
docker compose up --build     # first build ~5-10 min (Python deps + embedding model)
```

Open **http://localhost:8080** and pick a demo employee. The suggestion chips exercise every path in the graph.

| URL | What |
|---|---|
| http://localhost:8080 | Web app (nginx → 2 API replicas) |
| http://localhost:8080/api/docs | OpenAPI docs |
| http://localhost:8080/metrics | Prometheus metrics |
| `localhost:5433` | Postgres + pgvector (`support`/`support`) |

**LLM:** the agents call **Groq** using `GROQ_API_KEY`, `GROQ_MODEL_PRIMARY`, `GROQ_MODEL_FALLBACKS` and
`GROQ_MODEL_FALLBACK` from `.env`. Every call walks that model chain and fails over on errors or invalid
output. LiteLLM-style `groq/` prefixes are accepted. Without a key, or if every model fails, the same graph
runs on a deterministic offline path: keyword classification, regex entity extraction and extractive answers.
The trace shows which path each step used.

## The workflow

```
intake → authenticate → guardrails → classify → route ─┬─ clarify ─────────────────────┐
                                                       ├─ escalate (sensitive / human) ┤
                                                       ├─ small talk ──────────────────┤
                                                       └─ agent_<dept> → verify ───────┴→ [escalate] → respond
agent_<dept> subgraph:   plan ─┬─ retrieve → rerank → generate               (knowledge / RAG)
                               └─ validate_action → authorize → [approval] → execute   (action / tools)
```

| Spec requirement | Where |
|---|---|
| Intent / department / urgency / risk / entities | `classify` ([nodes.py](backend/app/orchestration/nodes.py)): Groq JSON-mode + Pydantic validation, heuristic fallback |
| Ambiguous & multi-domain requests | `route` → `clarify`; `secondary_departments` → follow-up tickets in `respond` |
| Config-driven department agents | [agents.yaml](backend/app/agents/agents.yaml) + versioned prompts in [prompts/v1](backend/app/agents/prompts/v1) |
| Hybrid RAG + access-aware retrieval | [search.py](backend/app/retrieval/search.py): pgvector cosine + Postgres FTS, RRF fusion, ACL in SQL, reranker |
| Grounded answers with citation validation | `generate` + `verify` (invalid / missing `[n]` lowers confidence) |
| Approved tools, input validation, RBAC/ABAC | [tools.py](backend/app/agents/tools.py): Pydantic arg schemas, roles, sensitivity rules |
| Human approval for sensitive actions | `approval` node: LangGraph `interrupt()`, checkpointed in Postgres, resumable on any replica |
| Idempotent tool calls | `execute`: idempotency key + `tool_invocations` log replay |
| Retries / timeouts / circuit breaker | [ticketing.py](backend/app/integrations/ticketing.py), per-node timeouts in [tracing.py](backend/app/orchestration/tracing.py), LLM model-chain failover |
| Escalation with context preserved | `escalate`: transcript + draft answer on the ticket, sensitive queues, "not resolved" feedback |
| Ticket lifecycle + legacy sync | [tickets.py](backend/app/services/tickets.py) → `mock-ticketing` service; failed syncs retried by the worker |
| Attachments: scan, extract, OCR | [tasks.py](backend/app/workers/tasks.py) (Celery): magic-byte + EICAR scan, PDF/DOCX text, Tesseract OCR |
| Guardrails | prompt-injection block, PII redaction before any LLM call, sensitive-topic detection |
| Observability | step trace per execution (UI + DB), JSON logs with correlation IDs, Prometheus metrics, optional OTLP traces |

## Things to try

| Employee | Prompt | What you'll see |
|---|---|---|
| Asha (IN) | *How many days of earned leave do I get per year?* | Retrieval reads the **India** addendum. Daniel (US) gets the US guide, and the trace notes documents hidden by policy. |
| Asha | *I need admin rights on GitHub for 4 hours* | `authorize` flags it as sensitive, `approval` pauses the graph (amber), and approving resumes it into `execute`. |
| Daniel | *Submit an expense claim*, then *it was $45 for a taxi* | `validate_action` asks for the missing fields, and the next turn resumes the pending plan. |
| Daniel | *Submit a $1,450 hotel expense for the Berlin trip* | Claims over 1,000 need manager approval. |
| anyone | *I'm being harassed by a colleague* | `route` sends it straight to `escalate`, into a confidential HR queue. |
| anyone | *Ignore previous instructions…* | `guardrails` blocks it, and `classify` never runs. |

Click **Show workflow trace** under any past answer to replay its execution in the left pane.

## Project layout

```
backend/app/
  api/            versioned REST + SSE endpoints (/api/v1)
  orchestration/  LangGraph state, nodes, graph wiring, tracing, runner
  agents/         agent registry (YAML), tool catalog, versioned prompts
  retrieval/      embeddings (fastembed bge-small, hash fallback), ingestion, hybrid search
  integrations/   ticketing adapter (retries, breaker), department API stand-ins
  llm/            Groq client with model-chain failover
  services/       tickets, audit, object storage
  workers/        Celery tasks
  db/             SQLAlchemy models, session, seed
backend/alembic/  migrations
backend/knowledge/  seed knowledge base (front matter carries ACLs)
frontend/         React + TypeScript (Vite), served by nginx
mock-ticketing/   stand-in for the existing ticketing platform (fault injection)
```

## Development

```bash
# backend unit tests
docker compose run --rm --no-deps api pytest -q tests/test_heuristics.py tests/test_retrieval_and_graph.py
# end-to-end workflow tests against the running stack
docker compose run --rm -e E2E_BASE_URL=http://web api pytest -q tests/test_e2e_api.py
# frontend with hot reload (proxies /api to the compose stack on :8080)
cd frontend && npm install && npm run dev        # http://localhost:5173
# chaos: make the legacy ticketing system fail 30% of calls
TICKETING_FAILURE_RATE=0.3 docker compose up -d mock-ticketing
```

## Local vs production

What this repo runs locally, and what changes for the 200k-employee deployment:

- **Auth:** `AUTH_MODE=dev` issues local JWTs for the seeded employees. Set `AUTH_MODE=oidc` with
  `OIDC_ISSUER`, `OIDC_AUDIENCE` and `OIDC_JWKS_URL` to validate corporate SSO tokens; employees are matched
  by email.
- **Approvals:** `ALLOW_SELF_APPROVAL=true` lets the requester act as approver for demos. Set it to `false`
  so only the manager or the designated role can approve.
- **Postgres:** a single container. Production should use managed HA Postgres with pgvector and PgBouncer.
- **Object storage:** a local Docker volume behind `LocalObjectStore`. Swap in S3, Blob or GCS.
- **Department APIs:** deterministic stand-ins in
  [department_apis.py](backend/app/integrations/department_apis.py). Replace them with real clients behind
  the same signatures.
- **AV scanning:** signature and type checks only. Plug ClamAV into `scan()`.
- **Load:** API replicas are stateless (`deploy.replicas`), and graph state lives in the checkpointer. Validate
  the 1,000-concurrent-user target with a load test before rollout.
