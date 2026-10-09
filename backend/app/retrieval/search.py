"""Access-aware hybrid retrieval: pgvector cosine + Postgres full-text, fused with Reciprocal Rank Fusion.

Document-level permissions are enforced inside the SQL, so a chunk the employee may not see never leaves the DB.
"""

import asyncio
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.retrieval.embeddings import get_embedder, to_pgvector

RRF_K = 60
CANDIDATES = 20
_WORD = re.compile(r"[a-zA-Z0-9]{3,}")
STOPWORDS = {
    "the", "and", "for", "with", "how", "what", "can", "you", "our", "are", "this", "that", "from", "have",
    "does", "need", "want", "please", "help", "would", "like", "about", "when", "where", "which", "there",
    "your", "will", "should", "could", "into", "they", "them", "who", "why", "get", "any", "has", "was",
}

ACL = """
    d.ingestion_status = 'ready'
    AND d.department = ANY(:collections)
    AND (cardinality(d.allowed_departments) = 0 OR :dept = ANY(d.allowed_departments))
    AND (cardinality(d.allowed_countries) = 0 OR :country = ANY(d.allowed_countries))
    AND (cardinality(d.allowed_roles) = 0 OR :role = ANY(d.allowed_roles))
"""

HYBRID_SQL = f"""
WITH vec AS (
    SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> CAST(:qvec AS vector)) AS rnk,
           1 - (c.embedding <=> CAST(:qvec AS vector)) AS sim
    FROM knowledge_chunks c JOIN knowledge_documents d ON d.id = c.document_id
    WHERE {ACL}
    ORDER BY c.embedding <=> CAST(:qvec AS vector)
    LIMIT {CANDIDATES}
),
fts AS (
    SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q) DESC) AS rnk
    FROM knowledge_chunks c JOIN knowledge_documents d ON d.id = c.document_id,
         to_tsquery('english', :tsq) q
    WHERE :tsq <> '' AND c.tsv @@ q AND {ACL}
    ORDER BY ts_rank_cd(c.tsv, q) DESC
    LIMIT {CANDIDATES}
),
fused AS (
    SELECT id, SUM(1.0 / ({RRF_K} + rnk)) AS rrf,
           MIN(rnk) FILTER (WHERE src = 'vec') AS vec_rank,
           MIN(rnk) FILTER (WHERE src = 'fts') AS fts_rank,
           MAX(sim) AS sim
    FROM (SELECT id, rnk, sim, 'vec' AS src FROM vec UNION ALL SELECT id, rnk, NULL, 'fts' FROM fts) u
    GROUP BY id
)
SELECT f.id AS chunk_id, f.rrf, f.vec_rank, f.fts_rank, f.sim, c.heading, c.content, c.chunk_index,
       d.id AS document_id, d.title, d.source, d.version, d.owner, d.sensitivity
FROM fused f
JOIN knowledge_chunks c ON c.id = f.id
JOIN knowledge_documents d ON d.id = c.document_id
ORDER BY f.rrf DESC
LIMIT :k
"""

# Counts documents in the agent's collections that this employee is NOT allowed to read (shown in the trace).
HIDDEN_SQL = f"""
SELECT count(*) FROM knowledge_documents d
WHERE d.department = ANY(:collections) AND d.ingestion_status = 'ready' AND NOT ({ACL})
"""


def query_terms(query: str) -> list[str]:
    return [w for w in (t.lower() for t in _WORD.findall(query)) if w not in STOPWORDS]


async def hybrid_search(
    db: AsyncSession, query: str, employee: dict, collections: list[str], k: int
) -> tuple[list[dict[str, Any]], int]:
    qvec = await asyncio.to_thread(get_embedder().embed_query, query)
    params = {
        "qvec": to_pgvector(qvec),
        "tsq": " | ".join(dict.fromkeys(query_terms(query))),
        "collections": collections,
        "dept": employee["department"],
        "country": employee["country"],
        "role": employee["role"],
        "k": k,
    }
    rows = (await db.execute(text(HYBRID_SQL), params)).mappings().all()
    hidden = (await db.execute(text(HIDDEN_SQL), params)).scalar_one()
    return [dict(r) for r in rows], int(hidden)


def rerank(query: str, hits: list[dict[str, Any]], keep: int) -> list[dict[str, Any]]:
    """Lightweight reranker: blends normalised fusion score, semantic similarity and query-term coverage.

    Swap for a cross-encoder when retrieval benchmarks justify the latency.
    """
    terms = set(query_terms(query))
    if not hits:
        return []
    max_rrf = max(h["rrf"] for h in hits) or 1.0
    for h in hits:
        body = f"{h['title']} {h.get('heading') or ''} {h['content']}".lower()
        coverage = (sum(1 for t in terms if t in body) / len(terms)) if terms else 0.0
        sim = float(h["sim"]) if h.get("sim") is not None else 0.0
        h["coverage"] = round(coverage, 3)
        h["score"] = round(0.35 * (h["rrf"] / max_rrf) + 0.35 * max(sim, 0.0) + 0.30 * coverage, 4)
    ranked = sorted(hits, key=lambda h: h["score"], reverse=True)
    return ranked[:keep]
