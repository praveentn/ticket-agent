from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import AppError
from app.core.security import current_employee
from app.db.models import Attachment, Employee
from app.db.session import get_db
from app.services.audit import audit
from app.services.storage import store
from app.workers.celery_app import celery_app

router = APIRouter(prefix="/attachments", tags=["attachments"])


def _view(a: Attachment) -> dict:
    return {"id": a.id, "filename": a.filename, "content_type": a.content_type, "size_bytes": a.size_bytes,
            "scan_status": a.scan_status, "extraction_status": a.extraction_status,
            "extracted_preview": (a.extracted_text or "")[:300]}


@router.post("", status_code=202)
async def upload(file: UploadFile = File(...), employee: Employee = Depends(current_employee),
                 db: AsyncSession = Depends(get_db)):
    ctype = (file.content_type or "").split(";")[0]
    if ctype not in settings.allowed_upload_types:
        raise AppError(415, "unsupported_type", f"Allowed types: {', '.join(settings.allowed_upload_types)}")
    data = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise AppError(413, "too_large", f"Maximum upload size is {settings.max_upload_mb} MB")
    if not data:
        raise AppError(422, "empty_file", "The file is empty")
    key = store.put(data, f"attachments/{employee.id}", file.filename or "upload")
    a = Attachment(employee_id=employee.id, filename=(file.filename or "upload")[:255], content_type=ctype,
                   size_bytes=len(data), storage_key=key)
    db.add(a)
    audit(db, employee.id, "attachment.uploaded", "attachment", a.id, filename=a.filename, size=len(data))
    await db.commit()
    celery_app.send_task("tasks.process_attachment", args=[a.id])  # scan → extract/OCR asynchronously
    return _view(a)


@router.get("/{attachment_id}")
async def status(attachment_id: str, employee: Employee = Depends(current_employee), db: AsyncSession = Depends(get_db)):
    a = await db.get(Attachment, attachment_id)
    if a is None or a.employee_id != employee.id:
        raise AppError(404, "not_found", "Attachment not found")
    return _view(a)
