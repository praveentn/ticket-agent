"""Document ingestion: parse front matter -> chunk by heading -> embed -> upsert with version provenance."""

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import KnowledgeChunk, KnowledgeDocument
from app.retrieval.embeddings import get_embedder

MAX_CHARS = 900
_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")


@dataclass
class ParsedDoc:
    title: str
    department: str
    body: str
    meta: dict = field(default_factory=dict)


def parse_markdown(text: str, default_title: str, default_department: str) -> ParsedDoc:
    meta: dict = {}
    m = _FRONT_MATTER.match(text)
    if m:
        meta = yaml.safe_load(m.group(1)) or {}
        text = text[m.end():]
    return ParsedDoc(
        title=meta.get("title", default_title),
        department=meta.get("department", default_department),
        body=text.strip(),
        meta=meta,
    )


def chunk_markdown(body: str) -> list[tuple[str | None, str]]:
    """Section-aware chunks: each chunk keeps its nearest heading; long sections split on paragraphs."""
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in body.splitlines():
        h = _HEADING.match(line)
        if h:
            sections.append((h.group(2).strip(), []))
        else:
            sections[-1][1].append(line)

    chunks: list[tuple[str | None, str]] = []
    for heading, lines in sections:
        paragraphs = [p.strip() for p in "\n".join(lines).split("\n\n") if p.strip()]
        buf = ""
        for p in paragraphs:
            if buf and len(buf) + len(p) > MAX_CHARS:
                chunks.append((heading, buf))
                buf = ""
            buf = f"{buf}\n\n{p}" if buf else p
        if buf:
            chunks.append((heading, buf))
    return chunks


def ingest_document(db: Session, *, source: str, text: str, default_department: str) -> tuple[KnowledgeDocument, str]:
    """Idempotent on (source, checksum). A changed checksum bumps the version and replaces chunks."""
    parsed = parse_markdown(text, Path(source).stem.replace("-", " ").title(), default_department)
    checksum = hashlib.sha256(text.encode()).hexdigest()

    doc = db.execute(select(KnowledgeDocument).where(KnowledgeDocument.source == source)).scalar_one_or_none()
    if doc and doc.checksum == checksum and doc.ingestion_status == "ready":
        return doc, "unchanged"

    meta = parsed.meta
    if doc is None:
        doc = KnowledgeDocument(source=source, version="1")
        db.add(doc)
        action = "created"
    else:
        doc.version = str(int(doc.version) + 1) if doc.version.isdigit() else doc.version + ".1"
        db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id))
        action = "updated"

    doc.title = parsed.title
    doc.department = parsed.department
    doc.owner = meta.get("owner", f"{parsed.department.upper()} Knowledge Team")
    doc.sensitivity = meta.get("sensitivity", "internal")
    doc.allowed_departments = meta.get("allowed_departments", []) or []
    doc.allowed_countries = meta.get("allowed_countries", []) or []
    doc.allowed_roles = meta.get("allowed_roles", []) or []
    doc.checksum = checksum
    db.flush()

    pieces = chunk_markdown(parsed.body)
    vectors = get_embedder().embed_passages([f"{parsed.title}. {h or ''}. {c}" for h, c in pieces])
    for i, ((heading, content), vec) in enumerate(zip(pieces, vectors)):
        db.add(KnowledgeChunk(document_id=doc.id, chunk_index=i, heading=heading, content=content, embedding=vec))
    doc.ingestion_status = "ready"
    db.flush()
    return doc, action
