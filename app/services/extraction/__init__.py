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

    # A row count and its confidence describe what WAS read; neither says
    # anything about what was not. A 24-page signed set with one page of
    # genuinely ruled cash flow figures can pass every check above -
    # several rows, every one of them read correctly - while the balance
    # sheet, the income statement and every note sit untouched, because
    # nothing here is built to read a full narrative set of accounts,
    # only a page shaped like one flat table. unread_content_ratio is
    # the measure that actually distinguishes the two: how much of the
    # document's own text sits on a page that produced nothing at all.
    # Half is a wide margin - the document that surfaced this measured
    # 97%, not a near miss either way - so a two-page trial balance with
    # a mostly-blank signature page never crosses it.
    if result.unread_content_ratio > 0.5:
        return True, (f"{result.unread_content_ratio:.0%} of this "
                      f"document's content is on pages that produced no "
                      f"rows at all — likely a multi-page set of signed "
                      f"accounts rather than a single table")

    return False, "rule-based extraction was reliable"


# A note number at the front of a heading: "1.", "(a)", "12 -", "iv)". The
# label has to be FOLLOWED by a separator to count as one, or the pattern eats
# the heading itself - it once took the first ten characters of every heading
# given to it, which left "Revenue" as "" and matched "Borrowings" to the
# dividends note.
_NOTE_NUMBER = re.compile(r"^\(?\s*(?:\d+|[ivxlcdm]+|[a-z])\s*[).:-]\s*")


# Wording a company's own notes use for a heading the library words another
# way. Matched after normalising, so plural and case do not matter.
_HEADING_SYNONYMS = {
    "loan from a bank": "borrowings",
    "bank loan": "borrowings",
    "loans": "borrowings",
    "general": "corporate information",
    "summary of material accounting policies": "material accounting policy information",
    "critical accounting judgements and key sources of estimation uncertainty":
        "significant accounting judgements and estimates",
    "profit before income tax": "profit before tax",
    "financial instruments and financial risks":
        "financial risk management objectives and policies",
    "capital management policies and objectives": "capital management",
    "assets pledged": "assets pledged as security",
    "trade and other receivables": "trade receivables",
    "other receivables": "trade receivables",
    "other payables": "trade and other payables",
}


def _fold(text: str) -> str:
    """A heading with plurals removed word by word."""
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w
                    for w in text.split())


def match_heading(title: str, library: dict):
    """The library key a company's note heading corresponds to, or None.

    Exact after normalising, then by a synonym the firm's own sets use, then
    ignoring plurals ("Finance cost" against "Finance costs"), then by close
    similarity. Exact matching alone left last year's "Finance cost",
    "Trade and other receivables" and "Loan from a bank" reported as having no
    note in this year's accounts while notes with those very names existed.
    """
    import difflib

    wanted = _normalise_heading(title)
    if not wanted:
        return None
    if wanted in library:
        return library[wanted]
    alias = _HEADING_SYNONYMS.get(wanted)
    if alias and alias in library:
        return library[alias]
    folded = {_fold(h): k for h, k in library.items()}
    if _fold(wanted) in folded:
        return folded[_fold(wanted)]
    close = difflib.get_close_matches(_fold(wanted), list(folded), n=1,
                                      cutoff=0.88)
    return folded[close[0]] if close else None


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
    # Against the library this engagement is pinned to, not the old flat one:
    # its headings differ ("Trade receivables" there, "Trade and other
    # receivables" in the versioned library), which left last year's
    # receivables note reported as having no note to go to.
    library = {}
    try:
        from ..reports import load_notes_catalogue
        library = {_normalise_heading(n["heading"]): n["key"]
                   for n in load_notes_catalogue(document.financial_year)}
    except Exception:                                      # noqa: BLE001
        log.exception("Could not read the pinned library for prior notes")
    if not library:
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
            matched_key=match_heading(note["title"], library),
            confidence=note["confidence"],
        ))

    log.info("Document %s: stored %d prior-year note(s)",
             document.id, len(outcome["notes"]))
    return len(outcome["notes"]), outcome.get("unreadable")


def _read_fixed_asset_register(document, path, file_type, raw_text) -> tuple:
    """Read and store per-asset detail from a fixed asset register.

    Returns (count, message) - how many assets were stored, and a short
    message for the caller to surface, or None. Never raises: a document
    whose figures read perfectly must not be marked failed because this
    second pass could not read it.
    """
    from datetime import date as date_cls
    from ..extraction.ai import extract_fixed_asset_register, ai_available
    from ...models import FixedAssetRegisterItem

    if not ai_available():
        return 0, None

    try:
        outcome = extract_fixed_asset_register(path, file_type, raw_text=raw_text)
    except Exception:                              # noqa: BLE001
        log.exception("Fixed asset register extraction raised")
        return 0, "The fixed asset register could not be read."

    if not outcome.get("ok"):
        return 0, (outcome.get("error")
                   or "The fixed asset register could not be read.")

    def _parse(value):
        try:
            return date_cls.fromisoformat(value) if value else None
        except ValueError:
            return None

    FixedAssetRegisterItem.query.filter_by(source_document_id=document.id).delete()

    stored = 0
    for asset in outcome["assets"]:
        if not asset.get("useful_life_years"):
            # No useful life given or inferable - nothing to check this
            # asset's depreciation against, so there is nothing to store
            # that the calculation could use.
            continue
        db.session.add(FixedAssetRegisterItem(
            financial_year_id=document.financial_year_id,
            source_document_id=document.id,
            description=asset["description"][:255],
            cost=asset["cost"],
            purchase_date=_parse(asset.get("purchase_date")),
            disposal_date=_parse(asset.get("disposal_date")),
            useful_life_years=asset["useful_life_years"],
            confidence=asset["confidence"],
        ))
        stored += 1

    log.info("Document %s: stored %d fixed asset(s)", document.id, stored)
    return stored, outcome.get("unreadable")


# Documents stating this year's balances, one account per line - the only
# ones the unknown-account test below can sensibly be run against. The trial
# balance is handled before it and so is not repeated here.
STATES_BALANCES = {"balance_sheet", "profit_and_loss", "general_ledger"}

# Documents read for what the company SAID, not only for what it counted.
# "signed_accounts" IS the prior-year category - there is no current-year
# signed accounts while this year's own are still being drafted, so there
# is no separate "prior_signed_accounts" to name here.
NOTE_BEARING = {"signed_accounts"}

# Read for per-asset detail a trial balance cannot carry - see
# _read_fixed_asset_register and services/depreciation_check.py.
ASSET_BEARING = {"fixed_asset_register", "prior_fixed_asset_register"}


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

    # A row the extractor itself was not confident about outranks every
    # category rule below - "supporting evidence" and "already in the
    # trial balance" both assume the row was read correctly, which is
    # exactly what a flagged row has not yet established. Signed accounts
    # is the sharpest case: it is excluded from STATES_BALANCES below
    # because it is not evidence to corroborate THIS year's accounts, but
    # that is precisely what let a flagged row in it verify itself
    # silently - it fed the comparative column and last year's mapping
    # with a figure nobody had actually confirmed.
    if any(item.needs_review and item.status == "auto"
           for item in document.line_items):
        return False, None

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
    # Set when the AI was needed because most of the document went unread,
    # and could not deliver. The few rows the rules did read are kept - but
    # they are a fragment, and "5 rows read" alone says the document is done.
    partial_read = None

    # The rule-based read of the document's own text, kept aside from
    # `result` because Stage 2 below can replace `result` wholesale with a
    # fresh ExtractionResult that was never given this text back - it only
    # returns figures, not the document's contents. Stage 3b/3c's second AI
    # pass (last year's note wording, a fixed asset register's per-asset
    # detail) needs the ORIGINAL text regardless of which stage the figures
    # ended up coming from.
    document_text = result.raw_text
    # Likewise the page count: an AI read returns a fresh result that never
    # had one, and identify_document needs the document's length to tell a
    # set of signed accounts from a trial balance.
    document_pages = result.page_count

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
        elif result.rows and ai_result.error:
            partial_read = (
                f"Only {len(result.rows)} row(s) could be read without the "
                f"AI, from part of the document - the rest was not read. "
                f"{ai_result.error} Press Re-extract to try again.")
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

    # An auditor's standing "the years print backwards" correction (see
    # documents.swap_years) applies to every fresh read of this document,
    # not just the one it was clicked on - otherwise the next re-extraction
    # (a different sheet, a re-read after adding an API key) silently
    # reverts to the original, wrong reading.
    if document.periods_swapped:
        for row in result.rows:
            row.period = "current" if row.period == "previous" else "previous"

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
        identified, identified_reason, _changed = identify_document(
            document, raw_text=document_text,
            page_count=result.page_count or document_pages)

    # --- Stage 3b: last year's words, not just its figures ------------------
    # Only for the signed accounts, and only when they carry narrative. The
    # comparative FIGURES are read above like any other document; this reads
    # what the company actually said in its notes, which nothing else does.
    notes_note = None
    notes_new = 0
    if document.category in NOTE_BEARING:
        notes_new, notes_note = _read_prior_year_notes(
            document, path, file_type, document_text)

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

    # --- Stage 3c: per-asset detail, not just the figure a class of asset
    # rolls up to. Only worth attempting when there is something to read -
    # a document with no rows at all was never going to hold a register.
    # See services/depreciation_check.py for what this feeds.
    assets_note = None
    if result.rows and document.category in ASSET_BEARING:
        _assets_new, assets_note = _read_fixed_asset_register(
            document, path, file_type, document_text)

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
    document.page_count = result.page_count or document_pages
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
        "partial_read": partial_read,
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
        # Same idea as notes_unreadable, for a fixed asset register: a row
        # this second pass could not read is not an asset that does not
        # exist, and only the preparer can tell the difference.
        "assets_unreadable": assets_note,
        # Why this document did not need a human before the accounts could
        # be built. None means it still does.
        "auto_verified": auto_verified,
        # Named accounts this document carries that the trial balance does
        # not - a difference to show, never a reason to stop.
        "differs": differs,
    }
