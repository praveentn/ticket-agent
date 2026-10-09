"""Environment-driven configuration. Every tunable lives here, never inline."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Agentic Support Desk"
    environment: str = "local"
    log_level: str = "INFO"

    # Persistence
    database_url: str = "postgresql+psycopg://support:support@postgres:5432/support"
    # LangGraph's checkpointer speaks plain psycopg, so it takes a libpq URL.
    checkpoint_database_url: str = "postgresql://support:support@postgres:5432/support"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    redis_url: str = "redis://redis:6379/0"

    # LLM (Groq). With no key the orchestrator uses its deterministic heuristic path.
    groq_api_key: str | None = None
    groq_model_primary: str = "openai/gpt-oss-120b"
    groq_model_fallback: str = "llama-3.1-8b-instant"
    # Comma-separated failover chain; LiteLLM-style "groq/" prefixes are accepted and stripped.
    groq_model_fallbacks: str = ""
    llm_max_tokens: int = 2048
    llm_timeout_seconds: float = 30.0

    # Auth: "dev" issues local JWTs for seeded demo employees; "oidc" validates corporate SSO tokens.
    auth_mode: str = "dev"
    jwt_secret: str = "change-me-in-any-shared-environment"
    jwt_ttl_minutes: int = 480
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_email_claim: str = "email"

    # API protections
    rate_limit_per_minute: int = 30
    max_message_chars: int = 4000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://localhost:8080"])

    # Agent workflow controls
    confidence_threshold: float = 0.55
    clarify_threshold: float = 0.35
    node_timeout_seconds: float = 90.0
    max_graph_steps: int = 40
    allow_self_approval: bool = True  # demo convenience: requester may act as their own approver

    # Retrieval
    embedding_provider: str = "fastembed"  # fastembed | hash
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    retrieval_top_k: int = 8
    rerank_keep: int = 4
    min_rerank_score: float = 0.18

    # Attachments / object storage
    object_store_dir: str = "/data/objects"
    max_upload_mb: int = 10
    allowed_upload_types: list[str] = Field(
        default_factory=lambda: [
            "image/png", "image/jpeg", "application/pdf", "text/plain", "text/markdown",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ]
    )

    # Enterprise integrations
    ticketing_base_url: str = "http://mock-ticketing:8100"
    ticketing_timeout_seconds: float = 5.0

    # Observability
    otel_exporter_otlp_endpoint: str | None = None

    @property
    def llm_enabled(self) -> bool:
        return bool(self.groq_api_key)

    @property
    def llm_model_chain(self) -> list[str]:
        """Primary model first, then fallbacks in order, de-duplicated."""
        raw = [self.groq_model_primary, *self.groq_model_fallbacks.split(","), self.groq_model_fallback]
        chain: list[str] = []
        for name in raw:
            name = name.strip().strip('"')
            if name.startswith("groq/"):
                name = name[len("groq/"):]
            if name and name not in chain:
                chain.append(name)
        return chain


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
