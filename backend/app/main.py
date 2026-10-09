import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import text

from app.api import attachments, auth, chat, conversations, feedback, knowledge, tickets, workflow
from app.config import settings
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import CorrelationIdMiddleware
from app.db.session import engine
from app.integrations.ticketing import breaker
from app.orchestration import runner
from app.orchestration.graph import build_graph

configure_logging(settings.log_level)
log = logging.getLogger(__name__)


def _setup_tracing(app: FastAPI) -> None:
    if not settings.otel_exporter_otlp_endpoint:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "support-api"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{settings.otel_exporter_otlp_endpoint}/v1/traces")))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = AsyncConnectionPool(
        conninfo=settings.checkpoint_database_url, max_size=20, open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    checkpointer = AsyncPostgresSaver(pool)  # tables are created by the one-shot `migrate` service
    runner.set_graph(build_graph().compile(checkpointer=checkpointer))
    log.info("workflow ready", extra={"llm": "groq" if settings.llm_enabled else "heuristic",
                                      "models": settings.llm_model_chain if settings.llm_enabled else []})
    yield
    await pool.close()
    await engine.dispose()


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Correlation-ID"])
install_error_handlers(app)
_setup_tracing(app)

v1 = APIRouter(prefix="/api/v1")
for r in (auth.router, chat.router, conversations.router, tickets.router, attachments.router, feedback.router,
          knowledge.router, workflow.router):
    v1.include_router(r)
app.include_router(v1)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
async def readyz():
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ready", "ticketing_circuit": breaker.state, "llm": "groq" if settings.llm_enabled else "heuristic"}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
