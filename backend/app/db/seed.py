"""Idempotent bootstrap: LangGraph checkpoint tables, demo employees, and the knowledge base.

Run by the one-shot `migrate` service after `alembic upgrade head`.
"""

import logging
from pathlib import Path

from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import select

from app.config import settings
from app.core.logging import configure_logging
from app.db.models import Employee
from app.db.session import SyncSessionLocal
from app.retrieval.ingest import ingest_document

log = logging.getLogger("seed")
KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"

EMPLOYEES = [
    # id, name, email, role, department, country, manager, title
    ("E1001", "Asha Rao", "asha.rao@acme.example", "employee", "engineering", "IN", "E2001", "Software Engineer"),
    ("E1002", "Daniel Kim", "daniel.kim@acme.example", "employee", "sales", "US", "E2002", "Account Executive"),
    ("E1003", "Sofia Müller", "sofia.mueller@acme.example", "employee", "finance", "DE", "E2002", "Financial Analyst"),
    ("E2001", "Priya Nair", "priya.nair@acme.example", "manager", "engineering", "IN", None, "Engineering Manager"),
    ("E2002", "Marcus Lee", "marcus.lee@acme.example", "manager", "sales", "US", None, "Regional Sales Director"),
    ("E3001", "Grace Okafor", "grace.okafor@acme.example", "hr_partner", "hr", "US", None, "HR Business Partner"),
    ("E3002", "Tom Becker", "tom.becker@acme.example", "it_admin", "it", "DE", None, "IT Operations Lead"),
]


def seed_employees(db) -> None:
    for eid, name, email, role, dept, country, mgr, title in EMPLOYEES:
        e = db.get(Employee, eid)
        if e is None:
            db.add(Employee(id=eid, name=name, email=email, role=role, department=dept, country=country,
                            manager_id=mgr, attributes={"title": title}))
    db.commit()


def seed_knowledge(db) -> None:
    for path in sorted(KNOWLEDGE_DIR.glob("*/*.md")):
        doc, action = ingest_document(db, source=f"kb://{path.parent.name}/{path.name}",
                                      text=path.read_text(encoding="utf-8"), default_department=path.parent.name)
        log.info("knowledge", extra={"doc": doc.title, "action": action, "version": doc.version})
    db.commit()


def main() -> None:
    configure_logging("INFO")
    with PostgresSaver.from_conn_string(settings.checkpoint_database_url) as saver:
        saver.setup()
    with SyncSessionLocal() as db:
        seed_employees(db)
        seed_knowledge(db)
        n = db.execute(select(Employee)).scalars().all()
        log.info("seed complete", extra={"employees": len(n)})


if __name__ == "__main__":
    main()
