"""Extraction orchestration.

Decides which engine handles a document and persists the result.

The policy, in one sentence: run the free deterministic parser first, and call
Claude only when that parser couldn't do the job well. Concretely —

    Clean .xlsx / .csv        -> rules only, no AI call, no cost
    .docx with real tables    -> rules only
    Typed PDF, tables found   -> rules; AI only if confidence came out low
    Scanned PDF / image       -> AI directly (no text layer to parse)
    Rules found nothing       -> AI fallback
    No API key configured     -> rules only, low-confidence rows flagged

Whatever the engine, every row lands in the same Review & Correct screen.
"""
import hashlib
import logging
from datetime import datetime
from pathlib import Path

from flask import current_app

from ...extensions import db
from ...models import Document, ExtractedLineItem
from .base import ExtractionResult, reconcile_trial_balance, score_row
from .parsers import detect_file_type, run_rule_based

log = logging.getLogger(__name__)

# Below this average confidence, a rule-based result is considered shaky
# enough to be worth a second opinion from Claude.
AI_FALLBACK_THRESHOLD = 0.70
# A "real" document should yield at least this many rows; fewer suggests the
# parser missed the table entirely.
MIN_EXPECTED_ROWS = 2


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _should_use_ai(result: ExtractionResult, file_type: str) -> tuple:
    """Decide whether to call Claude. Returns (use_ai, reason)."""
    from .ai import ai_available

    if not ai_available():
        return False, "no API key configured"

    if file_type == "image":
        return True, "image has no text layer"

    if result.error == "scanned":
        return True, "PDF has no extractable text (scanned)"

    if not result.rows:
        return True, "rule-based parser found no line items"

    if len(result.rows) < MIN_EXPECTED_ROWS:
        return True, f"only {len(result.rows)} row(s) found — likely missed the table"

    if result.confidence < AI_FALLBACK_THRESHOLD:
        return True, f"low rule-based confidence ({result.confidence:.2f})"

    return False, "rule-based extraction was reliable"


def extract_document(document_id: int) -> dict:
    """Extract one document end to end and save the line items.

    Safe to re-run: existing line items for the document are replaced.
    """
    document = db.session.get(Document, document_id)
    if document is None:
        return {"ok": False, "error": "document not found"}

    path = Path(document.storage_path)
    if not path.exists():
        document.extraction_status = "failed"
        document.extraction_error = "File missing from storage"
        db.session.commit()
        return {"ok": False, "error": "file missing"}

    document.extraction_status = "processing"
    db.session.commit()

    threshold = current_app.config.get("CONFIDENCE_THRESHOLD", 0.80)
    file_type = document.file_type or detect_file_type(document.original_filename)

    # --- Stage 1: deterministic parsing -------------------------------------
    result = run_rule_based(path, file_type,
                            sheets=document.source_sheets or None)
    engine_used = result.engine
    ai_used = False

    # --- Stage 2: AI fallback, only where it adds value ---------------------
    use_ai, reason = _should_use_ai(result, file_type)
    if use_ai:
        from .ai import extract_with_ai
        log.info("Document %s: using AI (%s)", document_id, reason)
        ai_result = extract_with_ai(path, file_type,
                                    category=document.category or "other",
                                    raw_text=result.raw_text)
        if ai_result.rows:
            result = ai_result
            engine_used = ai_result.engine or "ai"
            ai_used = True
        elif ai_result.error and not result.rows:
            document.extraction_status = "failed"
            document.extraction_error = ai_result.error
            db.session.commit()
            return {"ok": False, "error": ai_result.error}

    # --- Stage 3: score every row and persist -------------------------------
    for row in result.rows:
        score_row(row, engine_used, threshold)

    ExtractedLineItem.query.filter_by(document_id=document.id).delete()

    for index, row in enumerate(result.rows):
        db.session.add(ExtractedLineItem(
            document_id=document.id,
            row_index=index,
            raw_label=row.raw_label or row.label,
            raw_values=row.raw_values,
            label=row.label,
            account_code=row.account_code,
            account_type=row.account_type,
            amount=row.amount,
            debit=row.debit,
            credit=row.credit,
            period=row.period,
            confidence=row.confidence,
            needs_review=row.needs_review,
            source_ref=row.source_ref,
            status="auto",
        ))

    # What the document is, read from what is now inside it. Runs here
    # because this is the first moment the rows exist - and a file name only
    # ever guessed. An auditor's own choice is left alone.
    identified = identified_reason = None
    if result.rows:
        from ..identify import identify_document
        identified, identified_reason, _changed = identify_document(document)

    document.extraction_status = "extracted" if result.rows else "failed"
    document.extraction_engine = engine_used
    document.extraction_confidence = result.confidence
    document.extraction_error = None if result.rows else (
        result.error or "No line items found")
    document.ai_used = ai_used
    document.page_count = result.page_count
    if document.review_status == "pending" and result.rows:
        document.review_status = "in_review"

    db.session.commit()

    balance = reconcile_trial_balance(result.rows)
    return {
        "ok": bool(result.rows),
        "rows": len(result.rows),
        "engine": engine_used,
        "ai_used": ai_used,
        "ai_reason": reason if use_ai else None,
        "confidence": round(result.confidence, 3),
        "flagged": sum(1 for r in result.rows if r.needs_review),
        "balance": balance,
        "category": identified,
        "category_reason": identified_reason,
    }
