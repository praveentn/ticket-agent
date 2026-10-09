from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import load_agents
from app.core.errors import AppError
from app.core.security import current_employee
from app.db.models import Employee, KnowledgeChunk, KnowledgeDocument
from app.db.session import get_db
from app.retrieval.search import ACL
from app.services.audit import audit
from app.services.storage import store
from app.workers.celery_app import celery_app

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
CURATORS = {"it_admin", "hr_partner", "finance_analyst", "support_agent"}


@router.get("/documents")
async def list_documents(employee: Employee = Depends(current_employee), db: AsyncSession = Depends(get_db)):
    """Only documents this employee is permitted to read (same ACL as retrieval)."""
    params = {"collections": list(load_agents()), "dept": employee.department, "country": employee.country,
              "role": employee.role}
    ids = (await db.execute(text(f"SELECT d.id FROM knowledge_documents d WHERE {ACL}"), params)).scalars().all()
    counts = dict((await db.execute(
        select(KnowledgeChunk.document_id, func.count()).group_by(KnowledgeChunk.document_id))).all())
    docs = (await db.execute(select(KnowledgeDocument).where(KnowledgeDocument.id.in_(ids))
                             .order_by(KnowledgeDocument.department, KnowledgeDocument.title))).scalars()
    return [{"id": d.id, "title": d.title, "department": d.department, "version": d.version, "owner": d.owner,
             "sensitivity": d.sensitivity, "source": d.source, "chunks": counts.get(d.id, 0),
             "restrictions": {"departments": d.allowed_departments, "countries": d.allowed_countries,
                              "roles": d.allowed_roles}} for d in docs]


@router.post("/documents", status_code=202)
async def upload_document(
    department: str = Form(...), file: UploadFile = File(...),
    employee: Employee = Depends(current_employee), db: AsyncSession = Depends(get_db),
):
    if employee.role not in CURATORS:
        raise AppError(403, "forbidden", "Only knowledge curators can ingest documents")
    if department not in load_agents():
        raise AppError(422, "unknown_collection", f"department must be one of {sorted(load_agents())}")
    data = await file.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise AppError(413, "too_large", "Knowledge documents are limited to 5 MB")
    key = store.put(data, f"knowledge/{department}", file.filename or "doc.md")
    audit(db, employee.id, "knowledge.upload", "knowledge_document", key, department=department)
    await db.commit()
    celery_app.send_task("tasks.ingest_document", args=[key, department, file.filename or "doc.md",
                                                        file.content_type or "text/markdown"])
    return {"queued": True, "storage_key": key}
