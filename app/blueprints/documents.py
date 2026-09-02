"""Document upload, extraction and the Review & Correct screen."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, send_file, url_for)
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (DOCUMENT_CATEGORIES, Document, ExtractedLineItem,
                      FinancialYear, PriorYearNote, TrialBalanceAccount)
from ..services import storage
from ..services.audit import record
from ..services.categorise import detect_category
from ..services.extraction.base import reconcile_trial_balance
from ..services.jobs import enqueue

bp = Blueprint("documents", __name__, url_prefix="/documents")


def _load_document(document_id):
    document = db.session.get(Document, document_id) or abort(404)
    return document


@bp.route("/fy/<int:fy_id>/upload", methods=["GET", "POST"])
@login_required
def upload(fy_id):
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    if request.method == "POST":
        files = request.files.getlist("files")
        # "auto" reads each file's own name. A client sends the P&L, the
        # balance sheet and the ledger in one go, and one dropdown cannot
        # describe three documents - so without this they all arrive as
        # "other" and the trial balance builder, which decides what to build
        # FROM by category, finds nothing it recognises.
        chosen = request.form.get("category") or "auto"

        if not files or all(not f.filename for f in files):
            flash("Choose at least one file to upload.", "error")
            return redirect(request.url)

        saved, rejected = [], []

        for file_storage in files:
            if not file_storage.filename:
                continue

            if not storage.is_allowed(file_storage.filename):
                rejected.append(f"{file_storage.filename} (unsupported type)")
                continue

            try:
                meta = storage.save_upload(
                    file_storage, financial_year.customer_id, financial_year.id)
            except ValueError as exc:
                rejected.append(f"{file_storage.filename} ({exc})")
                continue

            document = Document(
                financial_year_id=financial_year.id,
                category=(detect_category(file_storage.filename)
                          if chosen == "auto" else chosen),
                category_source="filename" if chosen == "auto" else "manual",
                uploaded_by=current_user.id,
                extraction_status="queued",
                review_status="pending",
                **meta,
            )
            db.session.add(document)
            db.session.flush()
            saved.append(document)

        for document in saved:
            record("document", document.id, "upload",
                   after={"filename": document.original_filename})
        db.session.commit()

        # Extraction is deliberately NOT run here. The auditor uploads
        # everything first, checks the list, then presses Analyse - so a big
        # batch is processed in one deliberate step rather than trickling in
        # one file at a time.
        if saved:
            flash(f"Uploaded {len(saved)} document(s). Review the list, then "
                  f"press Analyse to read the figures out of them.", "success")
        if rejected:
            flash("Skipped: " + "; ".join(rejected), "warning")

        return redirect(url_for("documents.index", fy_id=financial_year.id))

    return render_template("documents/upload.html", fy=financial_year,
                           customer=financial_year.customer)


@bp.route("/fy/<int:fy_id>")
@login_required
def index(fy_id):
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    category = request.args.get("category") or ""
    status = request.args.get("status") or ""

    query = Document.query.filter_by(financial_year_id=fy_id)
    if category:
        query = query.filter_by(category=category)
    if status:
        query = query.filter_by(review_status=status)

    documents = query.order_by(Document.uploaded_at.desc()).all()

    # The unfiltered count, so the page can tell "this engagement has no
    # documents" from "no document matches this filter". Disabling the
    # filters on the filtered list would trap an auditor whose filter
    # happened to match nothing.
    total_documents = Document.query.filter_by(financial_year_id=fy_id).count()

    from ..services import xero as xero_service

    return render_template("documents/index.html",
                           fy=financial_year, customer=financial_year.customer,
                           documents=documents,
                           total_documents=total_documents,
                           category=category, status=status,
                           # This page is where an auditor answers "how do I
                           # get the figures in", so both channels - files
                           # and the accounting system - are offered here.
                           xero_available=xero_service.available(),
                           xero_demo=xero_service.demo_mode(),
                           xero_conn=xero_service.get_connection(
                               financial_year.customer_id))


@bp.route("/fy/<int:fy_id>/analyse", methods=["POST"])
@login_required
def analyse(fy_id):
    """Read the figures out of every un-analysed document in one go.

    This is the explicit step between uploading and reviewing: the auditor
    uploads a batch, checks the list is right, then presses Analyse.
    """
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    only_id = request.form.get("document_id", type=int)
    query = Document.query.filter_by(financial_year_id=fy_id)

    if only_id:
        query = query.filter_by(id=only_id)
    else:
        # Everything not already read successfully.
        retryable = ["queued", "failed"]

        # Inline extraction runs inside the request that started it, so a
        # document still marked "processing" by the time a later request
        # arrives is not in flight - it is stranded. That happens when the
        # process died mid-read (a large scanned PDF can outrun gunicorn's
        # timeout), and "processing" was already committed. Without this the
        # file disappears from Analyse and can never be read again.
        if current_app.config.get("JOBS_INLINE", True):
            retryable.append("processing")

        query = query.filter(Document.extraction_status.in_(retryable))

    documents = query.all()

    if not documents:
        flash("Nothing new to analyse. Upload a document, or use Re-extract on "
              "a specific file to read it again.", "warning")
        return redirect(url_for("documents.index", fy_id=fy_id))

    analysed = failed = rows = ai_count = 0
    unreadable_notes = []
    differences = []

    for document in documents:
        result = enqueue("extract_document", {"document_id": document.id})
        if result.get("ok"):
            analysed += 1
            rows += result.get("rows", 0)
            if result.get("ai_used"):
                ai_count += 1
        else:
            failed += 1

        # Reported even when the figures read perfectly: a note that could
        # not be read is not a note that was never disclosed, and only the
        # preparer can tell the difference.
        if result.get("notes_unreadable"):
            unreadable_notes.append(
                f"{document.original_filename}: {result['notes_unreadable']}")

        # A difference is a finding to show, not a reason to stop. The
        # accounts are built regardless; this names what did not match so
        # the preparer rules on it rather than hunting for it.
        if result.get("differs"):
            differences.append(
                f"{document.original_filename} — {result['differs']}")

    record("financial_year", fy_id, "analyse_documents",
           after={"analysed": analysed, "failed": failed, "rows": rows},
           commit=True)

    # The trial balance is read, not prepared. Where the documents already
    # carry one, the accounts are built here rather than behind a separate
    # button the preparer has to know to press - that step is the one the
    # firm said they could not understand the purpose of.
    built = None
    if analysed and not financial_year.tb_is_approved:
        from ..services import trial_balance as tb_service
        outcome = tb_service.build(fy_id, user_id=current_user.id)
        if outcome.get("ok"):
            built = outcome

            # A supporting document read BEFORE the trial balance existed had
            # nothing to be matched against and was left for review. Now
            # there is something, so it is asked again - otherwise the order
            # the files happened to be uploaded in decides how much work the
            # preparer is given.
            from ..services.extraction import auto_verify
            for document in financial_year.documents:
                if document.review_status != "in_review":
                    continue
                verified, why = auto_verify(document)
                if verified:
                    document.review_status = "verified"
                    document.reviewed_at = datetime.utcnow()
                elif why:
                    # First moment this can be said at all: before the build
                    # there was no trial balance to be absent from.
                    differences.append(
                        f"{document.original_filename} — {why}")
            db.session.commit()

    if analysed:
        message = (f"Analysed {analysed} document(s) and read {rows} line "
                   f"items.")
        if ai_count:
            message += f" {ai_count} needed AI to read."
        if built:
            message += (f" Extracted financials built: {built['accounts']} "
                        f"accounts from "
                        f"{', '.join(built['built_from'])}.")
            if built.get("unmapped"):
                message += (f" {built['unmapped']} still need mapping to a "
                            f"statement line.")
        flash(message, "success")
    if failed:
        flash(f"{failed} document(s) could not be read. Open each one to see "
              f"why.", "error" if not analysed else "warning")
    for warning in unreadable_notes:
        flash(f"Notes only partly read — {warning}", "warning")
    for difference in differences:
        flash(f"Difference to rule on — {difference}", "warning")

    return redirect(url_for("documents.index", fy_id=fy_id))


@bp.route("/<int:document_id>/review")
@login_required
def review(document_id):
    """Review & Correct: extracted data beside the original document."""
    document = _load_document(document_id)
    items = (ExtractedLineItem.query
             .filter_by(document_id=document.id)
             .order_by(ExtractedLineItem.row_index).all())

    # Give the auditor a running debit/credit check where it applies.
    #
    # A document's own "Total" row is excluded, exactly as it is when the
    # trial balance is built. Including it adds the whole document to itself
    # and reports a difference twice the real one - which sent an auditor
    # looking for an error of 101,580 that was really 50,790.
    from ..services.extraction.base import looks_like_total_label

    class _Row:
        def __init__(self, item):
            self.debit = item.debit
            self.credit = item.credit

    # A discarded row is one the auditor has already excluded, so it must
    # not keep counting against the check that tells them whether the
    # document balances - otherwise removing a duplicate appears to change
    # nothing, and the figure they are chasing never moves.
    # Last year's rows are excluded for the same reason the document's own
    # total is: they are not part of this year's arithmetic. Two years summed
    # together never balance, and the difference that produces is noise the
    # auditor would go looking for a cause of.
    def _live(rows):
        return [_Row(i) for i in rows
                if i.status != "discarded"
                and not looks_like_total_label(i.label)]

    balance = reconcile_trial_balance(
        _live([i for i in items if i.period != "previous"]))

    # Checked separately rather than not at all. Excluding last year from
    # this year's arithmetic is right; leaving its rows unverified is not.
    prior_rows = _live([i for i in items if i.period == "previous"])
    prior_balance = reconcile_trial_balance(prior_rows) if prior_rows else None

    flagged = sum(1 for i in items if i.needs_review and i.status == "auto")

    # Last year's note wording, where this is the signed accounts. Read-only
    # here: this screen is for checking what was read, not for editing it.
    prior_notes = (PriorYearNote.query
                   .filter_by(source_document_id=document.id)
                   .order_by(PriorYearNote.id).all())

    return render_template(
        "documents/review.html",
        document=document,
        fy=document.financial_year,
        customer=document.financial_year.customer,
        items=items, balance=balance, prior_balance=prior_balance,
        flagged=flagged, prior_notes=prior_notes,
    )


@bp.route("/<int:document_id>/file")
@login_required
def serve_file(document_id):
    """Stream the original file through an authenticated route.

    Files are never served statically — this check is what stops one
    customer's documents being reachable by guessing a URL.
    """
    document = _load_document(document_id)

    if document.storage_path.startswith("("):      # demo placeholder
        abort(404)

    try:
        path = storage.assert_within_storage(document.storage_path)
    except ValueError:
        abort(403)

    if not Path(path).exists():
        abort(404)

    return send_file(path, download_name=document.original_filename,
                     as_attachment=False)


@bp.route("/<int:document_id>/reextract", methods=["POST"])
@login_required
def reextract(document_id):
    """Run extraction again — useful after adding an API key."""
    document = _load_document(document_id)
    result = enqueue("extract_document", {"document_id": document.id})

    record("document", document.id, "reextract",
           after={"engine": result.get("engine"), "rows": result.get("rows")},
           commit=True)

    if result.get("ok"):
        engine = "AI (Claude)" if result.get("ai_used") else result.get("engine")
        flash(f"Re-extracted {result.get('rows')} rows using {engine}.", "success")
    else:
        flash(f"Extraction failed: {result.get('error')}", "error")

    return redirect(url_for("documents.review", document_id=document.id))


# --------------------------------------------------------------------------
# JSON API used by the review grid
# --------------------------------------------------------------------------

def _parse_decimal(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


@bp.route("/api/line-item/<int:item_id>", methods=["PATCH"])
@login_required
def update_line_item(item_id):
    """Save one auditor correction. Called on cell blur by review.js."""
    item = db.session.get(ExtractedLineItem, item_id) or abort(404)
    payload = request.get_json(silent=True) or {}

    before = {"label": item.label,
              "amount": str(item.amount) if item.amount is not None else None,
              "debit": str(item.debit) if item.debit is not None else None,
              "credit": str(item.credit) if item.credit is not None else None}

    if "label" in payload:
        item.label = (payload["label"] or "").strip()
    if "period" in payload:
        # Only ever the two the grid offers. Anything else would be silently
        # treated as current by the trial balance build, which is the one
        # direction this must never fail in.
        item.period = ("previous" if payload["period"] == "previous"
                       else "current")
    if "amount" in payload:
        item.amount = _parse_decimal(payload["amount"])
    if "debit" in payload:
        item.debit = _parse_decimal(payload["debit"])
    if "credit" in payload:
        item.credit = _parse_decimal(payload["credit"])
    if "account_code" in payload:
        item.account_code = (payload["account_code"] or "").strip() or None

    # An auditor has looked at this row, so it's no longer an unchecked guess.
    item.status = "corrected"
    item.needs_review = False
    item.confidence = 1.0
    item.corrected_by = current_user.id
    item.corrected_at = datetime.utcnow()

    after = {"label": item.label,
             "amount": str(item.amount) if item.amount is not None else None,
             "debit": str(item.debit) if item.debit is not None else None,
             "credit": str(item.credit) if item.credit is not None else None}

    record("line_item", item.id, "correct", before=before, after=after)
    db.session.commit()

    return jsonify({"ok": True, "status": item.status})


@bp.route("/api/line-item/<int:item_id>/accept", methods=["POST"])
@login_required
def accept_line_item(item_id):
    """Mark a flagged row as correct as-extracted."""
    item = db.session.get(ExtractedLineItem, item_id) or abort(404)
    item.status = "accepted"
    item.needs_review = False
    item.corrected_by = current_user.id
    item.corrected_at = datetime.utcnow()
    record("line_item", item.id, "accept")
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/api/line-item/<int:item_id>/discard", methods=["POST"])
@login_required
def discard_line_item(item_id):
    """Exclude a row (a header or footer the parser mistook for data)."""
    item = db.session.get(ExtractedLineItem, item_id) or abort(404)
    item.status = "discarded"
    item.needs_review = False
    item.corrected_by = current_user.id
    item.corrected_at = datetime.utcnow()
    record("line_item", item.id, "discard")
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/api/document/<int:document_id>/row", methods=["POST"])
@login_required
def add_row(document_id):
    """Add a line the extractor missed entirely."""
    document = _load_document(document_id)
    payload = request.get_json(silent=True) or {}

    highest = db.session.query(db.func.max(ExtractedLineItem.row_index)) \
        .filter_by(document_id=document.id).scalar() or 0

    item = ExtractedLineItem(
        document_id=document.id,
        row_index=highest + 1,
        label=(payload.get("label") or "").strip(),
        amount=_parse_decimal(payload.get("amount")),
        debit=_parse_decimal(payload.get("debit")),
        credit=_parse_decimal(payload.get("credit")),
        confidence=1.0, needs_review=False,
        status="corrected",
        source_ref={"source": "manual"},
        corrected_by=current_user.id,
        corrected_at=datetime.utcnow(),
    )
    db.session.add(item)
    record("line_item", None, "manual_add", after={"label": item.label})
    db.session.commit()

    return jsonify({"ok": True, "id": item.id, "row_index": item.row_index})


@bp.route("/<int:document_id>/verify", methods=["POST"])
@login_required
def verify(document_id):
    """Sign the document off so its data can feed the statements."""
    document = _load_document(document_id)

    outstanding = (ExtractedLineItem.query
                   .filter_by(document_id=document.id, needs_review=True,
                              status="auto").count())

    if outstanding:
        flash(f"{outstanding} flagged row(s) still need checking. Correct or "
              f"accept each one before verifying.", "error")
        return redirect(url_for("documents.review", document_id=document.id))

    document.review_status = "verified"
    document.reviewed_by = current_user.id
    document.reviewed_at = datetime.utcnow()

    record("document", document.id, "verify",
           after={"filename": document.original_filename})
    db.session.commit()

    flash(f"“{document.original_filename}” verified — its data will now feed "
          f"the financial statements.", "success")
    return redirect(url_for("documents.index", fy_id=document.financial_year_id))


@bp.route("/<int:document_id>/unverify", methods=["POST"])
@login_required
def unverify(document_id):
    """Reopen a signed-off document so its rows can be corrected again.

    Verifying locks a document, which is right - its figures feed the
    statements, and they should not move underneath a report without a
    deliberate act. But there was no way back, so an auditor who spotted a
    misread after signing off had to delete the file and start again.

    The trial balance is NOT rebuilt here. It still holds the old figures
    until the auditor rebuilds it, and the staleness banner says so.
    """
    document = _load_document(document_id)

    if document.financial_year.tb_is_approved:
        flash("The trial balance is approved and locked. Reopen it before "
              "changing the documents it was built from.", "error")
        return redirect(url_for("documents.review", document_id=document.id))

    document.review_status = "in_review"
    document.reviewed_by = None
    document.reviewed_at = None

    record("document", document.id, "unverify",
           after={"filename": document.original_filename})
    db.session.commit()

    flash(f"“{document.original_filename}” reopened for editing. Rebuild the "
          f"trial balance after you have finished correcting it.", "success")
    return redirect(url_for("documents.review", document_id=document.id))


@bp.route("/<int:document_id>/category", methods=["POST"])
@login_required
def recategorise(document_id):
    """Correct what a document is filed as.

    Needed because the category is not cosmetic: it decides which document
    the accounts are built FROM, and an engagement whose documents all say
    "other" reports that it has nothing to build from while showing the
    auditor a profit and loss and a balance sheet. Without this the only
    remedy is to delete every file and upload it again.

    Refused once the trial balance is approved, for the same reason deleting
    is: the statements and the audit report are generated from an approved
    trial balance, and changing which document built it afterwards would move
    figures that have already been reported.
    """
    document = db.session.get(Document, document_id) or abort(404)
    financial_year = document.financial_year
    fy_id = financial_year.id
    category = request.form.get("category") or "other"

    valid = {key for key, _label in DOCUMENT_CATEGORIES}
    if category not in valid:
        flash("That is not a document category.", "error")
        return redirect(url_for("documents.index", fy_id=fy_id))

    if financial_year.tb_is_approved:
        flash("The trial balance is approved. Reopen it before changing what "
              "a document is filed as.", "error")
        return redirect(url_for("documents.index", fy_id=fy_id))

    if category == document.category:
        return redirect(url_for("documents.index", fy_id=fy_id))

    before = document.category
    document.category = category
    # A person decided. Nothing automatic overwrites this afterwards -
    # not the file name, and not a later re-read of the contents.
    document.category_source = "manual"
    document.category_reason = None
    record("document", document.id, "recategorise",
           before={"category": before}, after={"category": category})
    db.session.commit()

    flash(f"“{document.original_filename}” is now filed as "
          f"{document.category_label}. Build the trial balance again for it "
          f"to take effect.", "success")
    return redirect(url_for("documents.index", fy_id=fy_id))


@bp.route("/<int:document_id>/delete", methods=["POST"])
@login_required
def delete(document_id):
    """Remove a document, but only while it is still unverified.

    A verified document's rows feed the trial balance and through it every
    printed figure. Deleting one would take those figures away without any
    trace of where they went, so the way back is deliberate: unverify the
    document first, which is itself refused once the trial balance is
    approved. The button is hidden in that state; this guard is what makes
    it true for a request that arrives without one.
    """
    document = _load_document(document_id)
    fy_id = document.financial_year_id
    filename = document.original_filename

    if document.review_status == "verified":
        flash(f"“{filename}” is verified and its figures feed the trial "
              f"balance. Reopen it with Unverify before deleting it.", "error")
        return redirect(url_for("documents.index", fy_id=fy_id))

    # A document that was verified once may already have put accounts into
    # the trial balance, and those rows point back at it. Two things follow.
    contributed = (TrialBalanceAccount.query
                   .filter_by(source_document_id=document.id))
    contributed_count = contributed.count()

    # An approved trial balance is the single source of truth for every
    # printed figure. Taking rows out of one is exactly what that forbids.
    if contributed_count and document.financial_year.tb_is_approved:
        flash(f"“{filename}” contributed {contributed_count} account(s) to an "
              f"approved trial balance. Reopen the trial balance before "
              f"deleting it.", "error")
        return redirect(url_for("documents.index", fy_id=fy_id))

    # Otherwise take its accounts out with it. They are source-derived and
    # come back on the next build; leaving them would strand rows pointing
    # at a document that no longer exists, which the database refuses.
    if contributed_count:
        contributed.delete(synchronize_session=False)
        db.session.flush()

    # Remove the file from disk too, not just the row.
    if not document.storage_path.startswith("("):
        try:
            path = storage.assert_within_storage(document.storage_path)
            Path(path).unlink(missing_ok=True)
        except ValueError:
            pass

    db.session.delete(document)
    record("document", document_id, "delete", before={"filename": filename})
    db.session.commit()

    if contributed_count:
        flash(f"Deleted “{filename}” and the {contributed_count} account(s) it "
              f"put into the trial balance. Rebuild the trial balance.",
              "success")
    else:
        flash(f"Deleted “{filename}”.", "success")
    return redirect(url_for("documents.index", fy_id=fy_id))


@bp.route("/<int:document_id>/sheets", methods=["GET", "POST"])
@login_required
def sheets(document_id):
    """Choose which sheets of a workbook hold the figures.

    A client's management-accounts workbook holds the general ledger, the
    GST computation, a receivables listing and the summary side by side.
    Reading all of them merges transaction detail into the trial balance;
    reading the wrong one produces figures that look plausible and are not
    the ones the accounts were built from.

    Auditmate defaults to a sheet named like a trial balance, but the name
    is not reliable - one real workbook calls a cash-movement summary
    "Trial Balance" while the audited figures sit on sheets named IS and BS.
    So the auditor decides, and the guess is only a starting point.
    """
    from ..services.extraction.parsers import list_sheets

    document = db.session.get(Document, document_id) or abort(404)

    if (document.file_type or "").lower() not in ("xlsx", "xls"):
        flash("Sheet selection applies to spreadsheets only.", "error")
        return redirect(url_for("documents.index",
                                fy_id=document.financial_year_id))

    path = Path(document.storage_path)
    if not path.exists():
        flash("That file is missing from storage.", "error")
        return redirect(url_for("documents.index",
                                fy_id=document.financial_year_id))

    if request.method == "POST":
        chosen = request.form.getlist("sheets")
        document.source_sheets = chosen or None

        if document.financial_year.tb_is_approved:
            flash("The trial balance is approved and locked. Reopen it "
                  "before changing what is read from this file.", "error")
            return redirect(url_for("documents.index",
                                    fy_id=document.financial_year_id))

        db.session.commit()

        from ..services.extraction import extract_document
        result = extract_document(document.id)

        if result.get("ok"):
            flash(f"Re-read {document.original_filename} from "
                  + (", ".join(chosen) if chosen else "all sheets")
                  + f" — {result.get('rows', 0)} row(s) found. "
                    f"Rebuild the trial balance to pick up the change.",
                  "success")
        else:
            flash(f"Could not re-read that file: "
                  f"{result.get('error', 'unknown error')}", "error")

        return redirect(url_for("documents.detail", document_id=document.id)
                        if "documents.detail" in current_app.view_functions
                        else url_for("documents.index",
                                     fy_id=document.financial_year_id))

    try:
        available = list_sheets(path)
    except Exception as exc:                        # noqa: BLE001
        flash(f"Could not read that workbook: {exc}", "error")
        return redirect(url_for("documents.index",
                                fy_id=document.financial_year_id))

    return render_template("documents/sheets.html",
                           document=document,
                           fy=document.financial_year,
                           customer=document.financial_year.customer,
                           sheets=available,
                           chosen=set(document.source_sheets or []))
