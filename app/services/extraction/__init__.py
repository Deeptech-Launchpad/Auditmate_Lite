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


# A note number at the front of a heading: "1.", "(a)", "12 -", "iv)". The
# label has to be FOLLOWED by a separator to count as one, or the pattern eats
# the heading itself - it once took the first ten characters of every heading
# given to it, which left "Revenue" as "" and matched "Borrowings" to the
# dividends note.
_NOTE_NUMBER = re.compile(r"^\(?\s*(?:\d+|[ivxlcdm]+|[a-z])\s*[).:-]\s*")


def _normalise_heading(text: str) -> str:
    """A note heading reduced to comparable words."""
    text = _NOTE_NUMBER.sub("", (text or "").lower().strip())
    text = re.sub(r"[^a-z ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _read_prior_year_notes(document, path, file_type, raw_text) -> str:
    """Read and store the note wording from a set of signed accounts.

    Returns (count, message) - how many notes were stored, and a short
    message for the caller to surface, or None. Never raises: a document whose
    figures read perfectly must not be marked failed because its narrative
    could not be read.
    """
    from ..extraction.ai import extract_prior_year_notes, ai_available
    from ...models import NoteLibraryEntry, PriorYearNote

    if not ai_available():
        return 0, None

    try:
        outcome = extract_prior_year_notes(path, file_type, raw_text=raw_text)
    except Exception:                              # noqa: BLE001
        log.exception("Prior-year note extraction raised")
        return 0, "The notes in this document could not be read."

    if not outcome.get("ok"):
        return 0, (outcome.get("error")
                   or "The notes in this document could not be read.")

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
    return len(outcome["notes"]), outcome.get("unreadable")


# Documents stating this year's balances, one account per line - the only
# ones the unknown-account test below can sensibly be run against. The trial
# balance is handled before it and so is not repeated here.
STATES_BALANCES = {"balance_sheet", "profit_and_loss", "general_ledger"}

# Documents read for what the company SAID, not only for what it counted.
NOTE_BEARING = {"signed_accounts", "prior_signed_accounts"}


def auto_verify(document) -> tuple:
    """Decide whether this document still needs a human to review it.

    The firm's position, and it is right: the trial balance is not something
    to prepare, it is something to read. Their accounting software already
    produced it, it already balances, and making them re-check every row of
    it on one screen before seeing the same rows on another is work this
    tool exists to remove.

    So the rule is about CONTENT, not document type:

      * The trial balance itself never needs review. It is the authority
        everything else is checked against.
      * A document whose accounts are already in the trial balance does not
        either. It is supporting detail for figures that already tally.
      * A document carrying accounts the trial balance has never heard of
        does - those would change the accounts, and that is a decision.

    Returns (verified, reason). Verified documents still appear in Review &
    Correct; nothing is hidden. They simply stop blocking the way forward.
    """
    from ...models import TrialBalanceAccount
    from .base import looks_like_total_label

    # Only the trial balance itself. A balance sheet or P&L can BUILD the
    # accounts when no trial balance was sent, but it is a presented
    # statement rather than the ledger's own listing, so it still gets a
    # look before it becomes the accounts.
    if document.category == "trial_balance":
        return True, "the trial balance itself - read, not prepared"

    # The test below asks whether a document names an account the trial
    # balance has never heard of. That question only means something for a
    # document that states THIS year's balances one account per line. A cash
    # flow statement reports movements, an invoice names a supplier, a bank
    # statement names a transaction - none of them can ever match an account
    # name, so every one of them would be held in review over a difference
    # the preparer cannot act on and that could never change the accounts.
    if document.category not in STATES_BALANCES:
        return True, "supporting evidence - it never becomes an account"

    accounts = TrialBalanceAccount.query.filter_by(
        financial_year_id=document.financial_year_id).all()
    if not accounts:
        # Nothing to match against yet. Not a failure - the trial balance
        # simply has not arrived. Left for review, and re-checked whenever
        # the document is read again.
        return False, None

    known_codes = {(a.account_code or "").strip().lower()
                   for a in accounts if a.account_code}
    known_names = {_normalise_heading(a.account_name) for a in accounts}

    unknown = []
    for item in document.line_items:
        if item.status == "discarded" or looks_like_total_label(item.label):
            continue
        code = (item.account_code or "").strip().lower()
        if code and code in known_codes:
            continue
        if _normalise_heading(item.label or "") in known_names:
            continue
        unknown.append(item.label)

    if unknown:
        # Named, not merely counted. "3 accounts need review" tells the
        # preparer to go looking; naming them is the finding itself, and
        # the firm asked for a difference to be shown rather than to be a
        # reason to stop - the accounts are still built either way.
        shown = ", ".join(f"“{label}”" for label in unknown[:3] if label)
        if len(unknown) > 3:
            shown += f" and {len(unknown) - 3} more"
        return False, (f"not in the trial balance: {shown}")

    return True, ("every account in it is already in the trial balance")


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
    ai_error = None

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
            # A set of signed accounts is read for its WORDING, and carries no
            # figures at all - so failing to find figures in one is not a
            # reason to stop before the stage that reads what it was sent for.
            if document.category not in NOTE_BEARING:
                document.extraction_status = "failed"
                document.extraction_error = ai_result.error
                db.session.commit()
                return {"ok": False, "error": ai_result.error}
            ai_error = ai_result.error

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
    notes_new = 0
    if document.category in NOTE_BEARING:
        notes_new, notes_note = _read_prior_year_notes(
            document, path, file_type, result.raw_text)

    # Read means read. Last year's signed accounts are wanted for their
    # WORDING, and a set of notes carries no figures at all - so judging the
    # document on line items alone reports a document that did exactly what
    # was asked of it as a failure, and hides the notes it did produce.
    notes_held = notes_new
    if notes_new == 0 and document.category in NOTE_BEARING:
        # A failed re-read must not erase the standing of one that worked.
        # The notes from the last good read are still stored - only a
        # successful read replaces them - so the document is not unread.
        from ...models import PriorYearNote
        notes_held = PriorYearNote.query.filter_by(
            source_document_id=document.id).count()

    # Two different questions, and answering only the first turned a run
    # where every call failed into a green success banner. What the document
    # HOLDS decides its status; what this RUN did decides what to say about
    # it, and a failure that changed nothing still has to be said out loud.
    read_now = bool(result.rows) or notes_new > 0
    got_something = bool(result.rows) or notes_held > 0
    failure = result.error or ai_error
    document.extraction_status = "extracted" if got_something else "failed"
    document.extraction_engine = engine_used
    document.extraction_confidence = result.confidence
    document.extraction_error = None if got_something else (
        failure or "No line items found")
    document.ai_used = ai_used
    document.page_count = result.page_count
    auto_verified = False
    differs = None
    # The rows above are still pending; auto_verify reads them back off the
    # document, so they have to be in the session's view first.
    db.session.flush()
    if document.review_status == "pending" and got_something:
        # A document the auditor has already ruled on is never re-decided
        # here; only one that has not been looked at yet.
        verified, why = auto_verify(document)
        if verified:
            document.review_status = "verified"
            document.reviewed_at = datetime.utcnow()
            auto_verified = why
        else:
            document.review_status = "in_review"
            differs = why

    db.session.commit()

    balance = reconcile_trial_balance(result.rows)
    return {
        # Notes count as a read. Judging on rows alone reported a set of
        # signed accounts that gave up all its wording as unreadable.
        "ok": got_something,
        # Set when this run read nothing new but the document still holds
        # what an earlier run read. The caller has to say so: silence here
        # reads as "done", and the auditor never learns the re-read failed.
        "unchanged": (failure or "Nothing new could be read.")
                     if got_something and not read_now else None,
        "notes": notes_new,
        "notes_held": notes_held,
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
        # Why this document did not need a human before the accounts could
        # be built. None means it still does.
        "auto_verified": auto_verified,
        # Named accounts this document carries that the trial balance does
        # not - a difference to show, never a reason to stop.
        "differs": differs,
    }
