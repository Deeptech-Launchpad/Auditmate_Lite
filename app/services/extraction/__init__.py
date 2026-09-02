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
import re
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


def _normalise_heading(text: str) -> str:
    """A note heading reduced to comparable words."""
    text = re.sub(r"^[\d\s.()a-z]{0,10}", "", (text or "").lower().strip())
    return re.sub(r"[^a-z ]+", " ", text).strip()


def _read_prior_year_notes(document, path, file_type, raw_text) -> str:
    """Read and store the note wording from a set of signed accounts.

    Returns a short message for the caller to surface, or None. Never raises:
    a document whose figures read perfectly must not be marked failed because
    its narrative could not be read.
    """
    from ..extraction.ai import extract_prior_year_notes, ai_available
    from ...models import NoteLibraryEntry, PriorYearNote

    if not ai_available():
        return None

    try:
        outcome = extract_prior_year_notes(path, file_type, raw_text=raw_text)
    except Exception:                              # noqa: BLE001
        log.exception("Prior-year note extraction raised")
        return "The notes in this document could not be read."

    if not outcome.get("ok"):
        return outcome.get("error") or "The notes in this document could not be read."

    # Match each note to a library entry by heading, so the preparer is shown
    # last year's wording against the right note. No match is fine and is
    # left null - a company-specific note our library never had is precisely
    # the one worth keeping.
    library = {_normalise_heading(entry.heading): entry.key
               for entry in NoteLibraryEntry.query.all()}

    PriorYearNote.query.filter_by(source_document_id=document.id).delete()

    for note in outcome["notes"]:
        db.session.add(PriorYearNote(
            financial_year_id=document.financial_year_id,
            source_document_id=document.id,
            note_number=note["note_number"],
            title=note["title"][:255],
            body_text=note["body_text"],
            matched_key=library.get(_normalise_heading(note["title"])),
            confidence=note["confidence"],
        ))

    log.info("Document %s: stored %d prior-year note(s)",
             document.id, len(outcome["notes"]))
    return outcome.get("unreadable")


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

    # --- Stage 3b: last year's words, not just its figures ------------------
    # Only for the signed accounts, and only when they carry narrative. The
    # comparative FIGURES are read above like any other document; this reads
    # what the company actually said in its notes, which nothing else does.
    notes_note = None
    if document.category in ("signed_accounts", "prior_signed_accounts"):
        notes_note = _read_prior_year_notes(document, path, file_type,
                                            result.raw_text)

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
        # Set when the notes were read but something in them could not be -
        # a faint scan, a missing page. Shown to the preparer rather than
        # swallowed, because a note silently absent reads as a note that was
        # never disclosed.
        "notes_unreadable": notes_note,
    }
