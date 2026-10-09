"""Background jobs: attachment scan + extraction/OCR, knowledge ingestion, ticket re-sync."""

import asyncio
import io
import logging

from app.db.models import Attachment, AuditEvent, Ticket
from app.db.session import SyncSessionLocal
from app.retrieval.ingest import ingest_document as _ingest
from app.services.storage import store
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
MAGIC = {
    "application/pdf": [b"%PDF"],
    "image/png": [b"\x89PNG"],
    "image/jpeg": [b"\xff\xd8\xff"],
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [b"PK\x03\x04"],
}


def scan(data: bytes, content_type: str) -> str:
    """Signature + type-confusion checks. Plug a real AV engine (e.g. ClamAV clamd) in here for production."""
    if EICAR in data:
        return "infected"
    expected = MAGIC.get(content_type)
    if expected and not any(data.startswith(m) for m in expected):
        return "rejected"  # declared type doesn't match file content
    return "clean"


def extract_text(data: bytes, content_type: str) -> str:
    if content_type == "application/pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:50])
    if content_type.startswith("image/"):
        import pytesseract
        from PIL import Image

        return pytesseract.image_to_string(Image.open(io.BytesIO(data)))  # OCR for screenshots
    if content_type.endswith("wordprocessingml.document"):
        import docx

        return "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)
    return data.decode("utf-8", errors="replace")


@celery_app.task(name="tasks.process_attachment", bind=True, max_retries=3, default_retry_delay=5)
def process_attachment(self, attachment_id: str) -> dict:
    with SyncSessionLocal() as db:
        a = db.get(Attachment, attachment_id)
        if a is None:
            return {"missing": attachment_id}
        data = store.get(a.storage_key)
        a.scan_status = scan(data, a.content_type)
        if a.scan_status != "clean":
            a.extraction_status = "skipped"
        else:
            try:
                a.extracted_text = extract_text(data, a.content_type).strip()[:20000]
                a.extraction_status = "done"
            except Exception as exc:
                log.exception("extraction failed")
                a.extraction_status = "failed"
                a.extracted_text = None
                db.add(AuditEvent(actor="worker", action="attachment.extraction_failed", resource_type="attachment",
                                  resource_id=a.id, details={"error": str(exc)[:300]}))
        db.add(AuditEvent(actor="worker", action="attachment.processed", resource_type="attachment", resource_id=a.id,
                          details={"scan": a.scan_status, "extraction": a.extraction_status}))
        db.commit()
        return {"id": a.id, "scan": a.scan_status, "extraction": a.extraction_status}


@celery_app.task(name="tasks.ingest_document", bind=True, max_retries=3, default_retry_delay=10)
def ingest_document(self, storage_key: str, department: str, filename: str, content_type: str) -> dict:
    data = store.get(storage_key)
    text = extract_text(data, content_type) if not filename.endswith((".md", ".txt")) else data.decode("utf-8", "replace")
    with SyncSessionLocal() as db:
        doc, action = _ingest(db, source=f"upload://{department}/{filename}", text=text, default_department=department)
        db.add(AuditEvent(actor="worker", action=f"knowledge.{action}", resource_type="knowledge_document",
                          resource_id=doc.id, details={"title": doc.title, "version": doc.version}))
        db.commit()
        return {"document_id": doc.id, "action": action, "version": doc.version}


@celery_app.task(name="tasks.sync_ticket", bind=True, max_retries=8)
def sync_ticket(self, ticket_id: str) -> dict:
    """Retries ticket sync to the legacy platform with exponential backoff (bounded)."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.config import settings
    from app.services.tickets import sync_external

    async def _run() -> str:
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                t = await db.get(Ticket, ticket_id)
                if t is None or t.sync_status == "synced":
                    return "noop"
                await sync_external(db, t, enqueue_retry=False)
                await db.commit()
                return t.sync_status
        finally:
            await engine.dispose()

    status = asyncio.run(_run())
    if status == "failed":
        raise self.retry(countdown=min(600, 30 * 2 ** self.request.retries))
    return {"ticket": ticket_id, "status": status}
