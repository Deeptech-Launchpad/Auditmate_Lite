"""Audit report builder, preview and PDF export."""
import io
from datetime import datetime

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, send_file, url_for)
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (AuditReport, AuditReportSection, FinancialYear,
                      ReportFigureOverride, StatementLine)
from ..services import provenance as provenance_service, readiness
from ..services import reports as report_service
from ..services import statements as statement_service
from ..services.audit import record

bp = Blueprint("reports", __name__, url_prefix="/reports")


@bp.context_processor
def inject_report_globals():
    """Statement captions, shared by the builder preview and the export."""
    return {"GROUP_HEADINGS": report_service.GROUP_HEADINGS,
            "OPTIONAL_LINES": report_service.optional_line_keys()}


def _assemble(report, chips=False):
    """Build the ordered list of renderable sections.

    `chips` wraps each substituted binding in an uneditable span, which the
    in-place editor needs and the delivered report must not have.
    """
    financial_year = report.financial_year
    customer = financial_year.customer

    return [report_service.section_payload(section, customer, financial_year,
                                           chips=chips)
            for section in report.sections
            if section.is_enabled]


@bp.route("/fy/<int:fy_id>")
@login_required
def builder(fy_id):
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    # The report is built from statements that follow the APPROVED trial
    # balance, so that approval is the gate - not a second sign-off on the
    # statements themselves.
    if not financial_year.tb_is_approved:
        return render_template("reports/locked.html",
                               fy=financial_year,
                               customer=financial_year.customer)

    report = report_service.ensure_report(financial_year)

    # A closed engagement renders the finished document, not an editor.
    editable = not financial_year.is_closed

    return render_template("reports/builder.html",
                           report=report, fy=financial_year,
                           editable=editable,
                           payloads=_assemble(report, chips=editable),
                           customer=financial_year.customer,
                           final_version=financial_year.final_version,
                           tb_approved_at=financial_year.tb_approved_at,
                           readiness=readiness.check(financial_year),
                           pdf_available=report_service.weasyprint_available())


@bp.route("/api/section/<int:section_id>", methods=["PATCH"])
@login_required
def update_section(section_id):
    """Toggle a section on/off, retitle it, or save its text."""
    section = db.session.get(AuditReportSection, section_id) or abort(404)
    payload = request.get_json(silent=True) or {}

    if "is_enabled" in payload:
        section.is_enabled = bool(payload["is_enabled"])
    if "title" in payload:
        section.title = (payload["title"] or section.title).strip()
    if "content_html" in payload:
        section.content_html = payload["content_html"]
    if "sort_order" in payload:
        section.sort_order = int(payload["sort_order"])

    if "labels" in payload:
        # Fixed captions on the cover page - "CORPORATE INFORMATION" and the
        # like. Merged rather than replaced so editing one caption cannot
        # discard the others, and a caption typed back to its default is
        # dropped rather than stored as a redundant override.
        binding = dict(section.data_binding or {})
        labels = dict(binding.get("labels") or {})
        for key, value in (payload["labels"] or {}).items():
            text = (value or "").strip()
            if text:
                labels[key] = text
            else:
                labels.pop(key, None)
        binding["labels"] = labels
        section.data_binding = binding

    record("report_section", section.id, "update",
           after={"enabled": section.is_enabled})
    db.session.commit()

    return jsonify({"ok": True})


def _decimal(raw):
    """Parse a figure typed into the report. Returns (value, error)."""
    from decimal import Decimal, InvalidOperation

    if raw is None or str(raw).strip() == "":
        return None, None                     # cleared: revert to computed
    cleaned = (str(raw).replace(",", "").replace("−", "-").strip())
    # Accountants write a negative as (1,234).
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1].strip()
    try:
        return Decimal(cleaned), None
    except InvalidOperation:
        return None, f"{raw!r} is not a number."


@bp.route("/api/line/<int:line_id>", methods=["PATCH"])
@login_required
def update_line(line_id):
    """Edit a statement line from inside the report.

    The wording and the figure are handled differently on purpose. A label is
    presentation - one client says Revenue, another Turnover - so it is simply
    stored. A figure is not: it is written through the same override path the
    Statements page uses, so the computed value is kept underneath, the change
    is recorded, and every dependent total is recalculated. Typing a number
    into the report can therefore never leave the report disagreeing with the
    statements behind it.
    """
    line = db.session.get(StatementLine, line_id) or abort(404)
    payload = request.get_json(silent=True) or {}
    before = {"label": line.effective_label, "amount": str(line.effective_amount)}

    if "label" in payload:
        text = (payload["label"] or "").strip()
        # Back to the template wording when cleared or typed back to it.
        line.label_override = None if (not text or text == line.label) else text

    if "amount" in payload:
        value, error = _decimal(payload["amount"])
        if error:
            return jsonify({"ok": False, "error": error}), 400
        line.manual_override_amount = value
        line.source = ("manual" if value is not None
                       else ("computed" if line.formula else "auto"))

    record("statement_line", line.id, "report_edit", before=before,
           after={"label": line.effective_label,
                  "amount": str(line.effective_amount)})
    db.session.commit()

    # Totals are formulas over these lines, so the whole statement is redone.
    statement_service.recalculate(line.statement_id)

    statement = line.statement
    return jsonify({
        "ok": True,
        "lines": [{"id": l.id,
                   "amount": float(l.effective_amount or 0),
                   "label": l.effective_label,
                   "overridden": l.is_overridden,
                   "label_overridden": l.label_is_overridden}
                  for l in statement.lines],
    })


@bp.route("/api/note-row", methods=["PATCH"])
@login_required
def update_note_row():
    """Edit one row of a note table.

    Note tables have no stored rows - they are recomputed from the trial
    balance on every render - so the edit is held by position and reapplied
    at render time. `anchor_label` records what the row said when it was
    edited, so a later rebuild that changes the note shows the edit as stale
    instead of moving it onto a different account.
    """
    payload = request.get_json(silent=True) or {}
    section = db.session.get(AuditReportSection,
                             int(payload.get("section_id", 0))) or abort(404)

    table_index = int(payload.get("table_index", 0))
    row_index = int(payload.get("row_index", 0))

    override = ReportFigureOverride.query.filter_by(
        report_id=section.report_id, section_key=section.section_key,
        table_index=table_index, row_index=row_index).first()

    if override is None:
        override = ReportFigureOverride(
            report_id=section.report_id, section_key=section.section_key,
            table_index=table_index, row_index=row_index,
            created_by=current_user.id)
        db.session.add(override)

    override.anchor_label = payload.get("anchor_label") or override.anchor_label

    if "label" in payload:
        text = (payload["label"] or "").strip()
        override.label_override = text or None

    if "amount" in payload:
        value, error = _decimal(payload["amount"])
        if error:
            return jsonify({"ok": False, "error": error}), 400
        override.amount_override = value

    # An override holding neither a label nor a figure is just clutter.
    if override.is_empty:
        db.session.delete(override)
        db.session.commit()
        return jsonify({"ok": True, "cleared": True})

    record("report_figure_override", override.id or 0, "note_edit",
           after={"section": section.section_key,
                  "label": override.label_override,
                  "amount": str(override.amount_override)})
    db.session.commit()
    return jsonify({"ok": True, "override_id": override.id})


@bp.route("/api/line/<int:line_id>/sources")
@login_required
def line_sources(line_id):
    """Which trial balance accounts make up this printed figure."""
    line = db.session.get(StatementLine, line_id) or abort(404)
    return jsonify({"ok": True, **provenance_service.for_statement_line(line)})


@bp.route("/api/account/<int:account_id>/sources")
@login_required
def account_sources(account_id):
    """Provenance for a note-table row that came from one account."""
    found = provenance_service.for_account(account_id)
    if found is None:
        abort(404)
    return jsonify({"ok": True, **found})


@bp.route("/api/fy/<int:fy_id>/coverage")
@login_required
def coverage(fy_id):
    """What in the trial balance never reached the report."""
    return jsonify(provenance_service.coverage(fy_id))


@bp.route("/api/fy/<int:fy_id>/suggest", methods=["POST"])
@login_required
def suggest(fy_id):
    """Propose a home for every account the report is missing.

    Mapping rules run first; only what they cannot place is sent to the AI,
    and only account names go - never figures, never the client's name.
    """
    use_ai = bool((request.get_json(silent=True) or {}).get("use_ai", True))
    return jsonify(provenance_service.suggest(fy_id, use_ai=use_ai))


@bp.route("/api/report/<int:report_id>/reorder", methods=["POST"])
@login_required
def reorder(report_id):
    """Persist a drag-and-drop reorder of the sections."""
    report = db.session.get(AuditReport, report_id) or abort(404)
    order = (request.get_json(silent=True) or {}).get("order", [])

    lookup = {s.id: s for s in report.sections}
    for position, section_id in enumerate(order):
        section = lookup.get(int(section_id))
        if section:
            section.sort_order = position

    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/fy/<int:fy_id>/finalise", methods=["POST"])
@login_required
def finalise(fy_id):
    """Sign the report off and close the engagement.

    This is the last act on a file: the report is issued, so the engagement
    stops being work in progress. It locks the report against further
    editing for the same reason the trial balance locks on approval - a
    document that has been issued and then quietly edited is worse than no
    version control at all.

    Reversible by design (see `reopen`). An engagement closed by mistake
    should not need someone in the database.
    """
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    if financial_year.is_closed:
        flash("This engagement is already closed.", "warning")
        return redirect(url_for("reports.builder", fy_id=fy_id))

    if not financial_year.tb_is_approved:
        flash("Approve the trial balance first - the report is built from "
              "it, so it cannot be signed off before the figures are.",
              "error")
        return redirect(url_for("trial_balance.index", fy_id=fy_id))

    report = financial_year.report
    if report is None:
        flash("Generate the audit report before closing the engagement.",
              "error")
        return redirect(url_for("reports.builder", fy_id=fy_id))

    note = (request.form.get("note") or "").strip() or None

    report.status = "final"
    if report.generated_at is None:
        report.generated_at = datetime.utcnow()
        report.generated_by = current_user.id

    financial_year.status = "closed"
    financial_year.closed_at = datetime.utcnow()
    financial_year.closed_by = current_user.id
    financial_year.closed_note = note

    record("financial_year", financial_year.id, "close",
           after={"status": "closed", "note": note}, commit=True)

    flash(f"{financial_year.customer.name} {financial_year.year_label} is "
          f"closed. The report is locked; reopen it if it needs changing.",
          "success")
    return redirect(url_for("reports.builder", fy_id=fy_id))


@bp.route("/fy/<int:fy_id>/reopen", methods=["POST"])
@login_required
def reopen(fy_id):
    """Put a closed engagement back into work."""
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    if not financial_year.is_closed:
        flash("That engagement is not closed.", "warning")
        return redirect(url_for("reports.builder", fy_id=fy_id))

    financial_year.status = "report_generated"
    financial_year.closed_at = None
    financial_year.closed_by = None
    financial_year.closed_note = None

    record("financial_year", financial_year.id, "reopen",
           after={"status": "report_generated"}, commit=True)

    flash("Engagement reopened. The report is editable again.", "success")
    return redirect(url_for("reports.builder", fy_id=fy_id))


@bp.route("/<int:report_id>/preview")
@login_required
def preview(report_id):
    report = db.session.get(AuditReport, report_id) or abort(404)

    return render_template("reports/preview.html",
                           report=report,
                           fy=report.financial_year,
                           customer=report.financial_year.customer,
                           payloads=_assemble(report),
                           for_pdf=False)


@bp.route("/<int:report_id>/export/word")
@login_required
def export_word(report_id):
    """The unaudited financial statements as an editable Word document.

    This is the deliverable. The firm finishes it by hand - changing a
    figure, rewriting a note, adding one that was never in the list - which
    is exactly why it is a .docx and not a PDF.

    It converts the same HTML the preview renders, so what is on screen and
    what lands in Word cannot drift apart.
    """
    from ..services import docx_export

    report = db.session.get(AuditReport, report_id) or abort(404)
    financial_year = report.financial_year

    html = render_template("reports/preview.html",
                           report=report,
                           fy=financial_year,
                           customer=financial_year.customer,
                           payloads=_assemble(report),
                           for_pdf=True)

    try:
        data = docx_export.build(html)
    except Exception as exc:                        # noqa: BLE001
        flash(f"Word export failed: {exc}", "error")
        return redirect(url_for("reports.preview", report_id=report.id))

    report.status = "final"
    report.generated_at = datetime.utcnow()
    report.generated_by = current_user.id
    if financial_year.status in ("in_progress", "statements_shared", "approved"):
        financial_year.status = "report_generated"

    record("audit_report", report.id, "export_word")
    db.session.commit()

    filename = (f"{financial_year.customer.name}_{financial_year.year_label}"
                f"_Unaudited_Financial_Statements.docx").replace(" ", "_")

    return send_file(
        io.BytesIO(data),
        mimetype=("application/vnd.openxmlformats-officedocument"
                  ".wordprocessingml.document"),
        as_attachment=True, download_name=filename)


@bp.route("/<int:report_id>/export")
@login_required
def export(report_id):
    """Export to PDF, or fall back to the printable page."""
    report = db.session.get(AuditReport, report_id) or abort(404)

    html = render_template("reports/preview.html",
                           report=report,
                           fy=report.financial_year,
                           customer=report.financial_year.customer,
                           payloads=_assemble(report),
                           for_pdf=True)

    if not report_service.weasyprint_available():
        # WeasyPrint isn't installed (typical on Windows dev machines) — send
        # the user to the printable page instead of failing.
        flash("PDF engine not installed — use your browser's Print → Save as "
              "PDF on this page. (Install WeasyPrint on the server for "
              "one-click export.)", "warning")
        return redirect(url_for("reports.preview", report_id=report.id))

    try:
        pdf_bytes = report_service.render_pdf(html, base_url=request.url_root)
    except Exception as exc:                       # noqa: BLE001
        flash(f"PDF generation failed: {exc}", "error")
        return redirect(url_for("reports.preview", report_id=report.id))

    report.status = "final"
    report.generated_at = datetime.utcnow()
    report.generated_by = current_user.id

    financial_year = report.financial_year
    # Only move forward. A closed engagement stays closed, and an engagement
    # still working through customer review is not dragged past that by an
    # export - but one that has simply skipped the statements-version chain
    # should not be stuck at "In Progress" forever either.
    if financial_year.status in ("in_progress", "statements_shared", "approved"):
        financial_year.status = "report_generated"

    record("audit_report", report.id, "export_pdf")
    db.session.commit()

    filename = (f"{financial_year.customer.name}_{financial_year.year_label}"
                f"_Audit_Report.pdf").replace(" ", "_")

    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf",
                     as_attachment=True, download_name=filename)
