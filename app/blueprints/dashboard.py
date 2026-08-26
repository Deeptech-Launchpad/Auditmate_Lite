"""Dashboard — the post-login landing page."""
from flask import Blueprint, render_template
from flask_login import login_required
from sqlalchemy import func

from ..extensions import db
from ..models import (AuditLog, AuditReport, Customer, Document,
                      FinancialStatement, FinancialYear)

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@login_required
def index():
    metrics = {
        "customers": Customer.query.filter_by(is_active=True).count(),
        "documents_pending": Document.query.filter(
            Document.review_status.in_(["pending", "in_review"])).count(),
        "statements_awaiting": FinancialYear.query.filter_by(
            status="statements_shared").count(),
        "reports_generated": AuditReport.query.count(),
    }

    # Documents that extracted but still have rows the auditor hasn't checked.
    needs_attention = (Document.query
                       .filter(Document.review_status == "in_review")
                       .order_by(Document.uploaded_at.desc())
                       .limit(6).all())

    # Everything still open. Listing by what an engagement is NOT means
    # exporting a PDF no longer makes a live engagement disappear from the
    # dashboard - closing it is what takes it off, which is the whole point
    # of having a closed state.
    active_years = (FinancialYear.query
                    .join(Customer)
                    .filter(FinancialYear.status != "closed")
                    .order_by(FinancialYear.updated_at.desc())
                    .limit(8).all())

    recent_activity = (AuditLog.query
                       .order_by(AuditLog.created_at.desc())
                       .limit(10).all())

    # How much of the extraction workload the AI handled.
    ai_docs = Document.query.filter_by(ai_used=True).count()
    total_docs = Document.query.count()

    return render_template(
        "dashboard/index.html",
        metrics=metrics,
        needs_attention=needs_attention,
        active_years=active_years,
        recent_activity=recent_activity,
        ai_docs=ai_docs,
        total_docs=total_docs,
    )
