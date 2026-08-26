"""Background job queue.

Extraction can take a while (a large scanned PDF sent to Claude is not
instant), so it runs as a job rather than blocking the upload request.

Two modes, controlled by JOBS_INLINE in .env:
  JOBS_INLINE=1  run the job immediately in-process. Simple, good for demos.
  JOBS_INLINE=0  queue it for worker.py, which claims rows with
                 SELECT ... FOR UPDATE SKIP LOCKED so several workers can run
                 side by side without processing the same job twice.
"""
import logging
from datetime import datetime

from flask import current_app
from sqlalchemy import text

from ..extensions import db
from ..models import Job

log = logging.getLogger(__name__)

HANDLERS = {}


def handler(job_type):
    def decorator(func):
        HANDLERS[job_type] = func
        return func
    return decorator


@handler("extract_document")
def _handle_extract(payload):
    from .extraction import extract_document
    document_id = payload["document_id"]
    try:
        return extract_document(document_id)
    except Exception:                              # noqa: BLE001
        # extract_document commits "processing" before it starts reading, so
        # a crash after that point leaves the document in a state Analyse
        # does not pick up again - the file goes quiet and can never be
        # retried. Record the failure so it can.
        db.session.rollback()
        from ..models import Document
        document = db.session.get(Document, document_id)
        if document is not None and document.extraction_status == "processing":
            document.extraction_status = "failed"
            document.extraction_error = (
                "Reading the file failed part-way through. Try Re-extract; "
                "if it keeps failing the file may be too large or corrupt.")
            db.session.commit()
        raise


def enqueue(job_type: str, payload: dict) -> dict:
    """Queue a job, or run it now if JOBS_INLINE is set."""
    if current_app.config.get("JOBS_INLINE", True):
        func = HANDLERS.get(job_type)
        if func is None:
            return {"ok": False, "error": f"no handler for {job_type}"}
        try:
            return func(payload)
        except Exception as exc:                   # noqa: BLE001
            log.exception("Inline job %s failed", job_type)
            db.session.rollback()
            return {"ok": False, "error": str(exc)}

    job = Job(job_type=job_type, payload=payload, status="queued")
    db.session.add(job)
    db.session.commit()
    return {"ok": True, "queued": True, "job_id": job.id}


def claim_next():
    """Claim one queued job atomically (worker process only).

    SKIP LOCKED lets multiple workers pull from the same table without
    blocking each other or double-processing a row.
    """
    row = db.session.execute(text("""
        SELECT id FROM jobs
        WHERE status = 'queued'
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    """)).first()

    if row is None:
        return None

    job = db.session.get(Job, row[0])
    job.status = "processing"
    job.started_at = datetime.utcnow()
    job.attempts = (job.attempts or 0) + 1
    db.session.commit()
    return job


def run_job(job: Job) -> None:
    func = HANDLERS.get(job.job_type)
    if func is None:
        job.status = "failed"
        job.error = f"no handler for {job.job_type}"
    else:
        try:
            result = func(job.payload or {})
            job.status = "done" if result.get("ok") else "failed"
            job.error = result.get("error")
        except Exception as exc:                   # noqa: BLE001
            log.exception("Job %s failed", job.id)
            db.session.rollback()
            job.status = "failed"
            job.error = str(exc)

    job.finished_at = datetime.utcnow()
    db.session.commit()
