"""Audit report builder, preview and PDF export."""
import io
from datetime import datetime

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, send_file, url_for)
from flask_login import current_user, login_required

from ..extensions import db
from ..models import AuditReport, AuditReportSection, FinancialYear
from ..services import reports as report_service
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

    return render_template("reports/builder.html",
                           report=report, fy=financial_year,
                           payloads=_assemble(report, chips=True),
                           customer=financial_year.customer,
                           final_version=financial_year.final_version,
                           tb_approved_at=financial_year.tb_approved_at,
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

    record("report_section", section.id, "update",
           after={"enabled": section.is_enabled})
    db.session.commit()

    return jsonify({"ok": True})


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
    if financial_year.status == "approved":
        financial_year.status = "report_generated"

    record("audit_report", report.id, "export_pdf")
    db.session.commit()

    filename = (f"{financial_year.customer.name}_{financial_year.year_label}"
                f"_Audit_Report.pdf").replace(" ", "_")

    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf",
                     as_attachment=True, download_name=filename)
