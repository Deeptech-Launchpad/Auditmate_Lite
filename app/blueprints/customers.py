"""Customer management and financial-year workspace."""
from datetime import date, datetime, timedelta

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)
from flask_login import current_user, login_required
from sqlalchemy import or_

from ..extensions import db
from ..models import Customer, Document, FinancialStatement, FinancialYear
from ..services.audit import record

bp = Blueprint("customers", __name__, url_prefix="/customers")


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


@bp.route("/")
@login_required
def index():
    search = (request.args.get("q") or "").strip()
    page = request.args.get("page", 1, type=int)

    query = Customer.query
    if search:
        pattern = f"%{search}%"
        query = query.filter(or_(Customer.name.ilike(pattern),
                                 Customer.uen.ilike(pattern),
                                 Customer.email.ilike(pattern),
                                 Customer.contact_person.ilike(pattern)))

    pagination = (query.order_by(Customer.name)
                  .paginate(page=page, per_page=20, error_out=False))

    return render_template("customers/index.html",
                           pagination=pagination, search=search)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Customer name is required.", "error")
            return render_template("customers/form.html",
                                   customer=None, form=request.form), 400

        customer = Customer(
            name=name,
            legal_name=(request.form.get("legal_name") or "").strip() or None,
            entity_type=request.form.get("entity_type") or "private_limited",
            uen=(request.form.get("uen") or "").strip() or None,
            gst_reg_no=(request.form.get("gst_reg_no") or "").strip() or None,
            email=(request.form.get("email") or "").strip() or None,
            phone=(request.form.get("phone") or "").strip() or None,
            contact_person=(request.form.get("contact_person") or "").strip() or None,
            directors=(request.form.get("directors") or "").strip() or None,
            company_secretary=(request.form.get("company_secretary") or "").strip() or None,
            address_line1=(request.form.get("address_line1") or "").strip() or None,
            address_line2=(request.form.get("address_line2") or "").strip() or None,
            postal_code=(request.form.get("postal_code") or "").strip() or None,
            country=(request.form.get("country") or "Singapore").strip(),
            books_currency=(request.form.get("books_currency") or "SGD").strip(),
            financial_year_end_month=request.form.get(
                "financial_year_end_month", 12, type=int),
            notes=(request.form.get("notes") or "").strip() or None,
            created_by=current_user.id,
            engagement_partner_id=current_user.id,
        )

        incorporation = request.form.get("incorporation_date")
        if incorporation:
            try:
                customer.incorporation_date = datetime.strptime(
                    incorporation, "%Y-%m-%d").date()
            except ValueError:
                pass

        db.session.add(customer)
        db.session.flush()

        # Create the first financial year alongside the customer, so the
        # workspace is immediately usable.
        year_label = (request.form.get("year_label") or "").strip()
        if year_label:
            fye_month = customer.financial_year_end_month or 12
            try:
                year_number = int("".join(c for c in year_label if c.isdigit())[:4])
            except ValueError:
                year_number = date.today().year

            end = _month_end(year_number, fye_month)
            start = date(end.year - 1, end.month, 1) if fye_month != 12 \
                else date(year_number, 1, 1)

            db.session.add(FinancialYear(
                customer_id=customer.id, year_label=year_label,
                start_date=start, end_date=end, status="in_progress"))

        record("customer", customer.id, "create", after={"name": customer.name})
        db.session.commit()

        flash(f"Customer “{customer.name}” created.", "success")
        return redirect(url_for("customers.detail", customer_id=customer.id))

    default_label = f"FY{date.today().year}"
    return render_template("customers/form.html", customer=None,
                           form={}, default_label=default_label)


@bp.route("/<int:customer_id>")
@login_required
def detail(customer_id):
    customer = db.session.get(Customer, customer_id) or abort(404)
    return render_template("customers/detail.html", customer=customer)


@bp.route("/<int:customer_id>/edit", methods=["GET", "POST"])
@login_required
def edit(customer_id):
    customer = db.session.get(Customer, customer_id) or abort(404)

    if request.method == "POST":
        before = {"name": customer.name, "uen": customer.uen}

        customer.name = (request.form.get("name") or customer.name).strip()
        customer.legal_name = (request.form.get("legal_name") or "").strip() or None
        customer.entity_type = request.form.get("entity_type") or customer.entity_type
        customer.uen = (request.form.get("uen") or "").strip() or None
        customer.gst_reg_no = (request.form.get("gst_reg_no") or "").strip() or None
        customer.email = (request.form.get("email") or "").strip() or None
        customer.phone = (request.form.get("phone") or "").strip() or None
        customer.contact_person = (request.form.get("contact_person") or "").strip() or None
        customer.directors = (request.form.get("directors") or "").strip() or None
        customer.company_secretary = (request.form.get("company_secretary") or "").strip() or None
        customer.address_line1 = (request.form.get("address_line1") or "").strip() or None
        customer.address_line2 = (request.form.get("address_line2") or "").strip() or None
        customer.postal_code = (request.form.get("postal_code") or "").strip() or None
        customer.country = (request.form.get("country") or "Singapore").strip()
        customer.books_currency = (request.form.get("books_currency") or "SGD").strip()
        customer.financial_year_end_month = request.form.get(
            "financial_year_end_month", 12, type=int)
        customer.notes = (request.form.get("notes") or "").strip() or None

        record("customer", customer.id, "update", before=before,
               after={"name": customer.name, "uen": customer.uen})
        db.session.commit()

        flash("Customer updated.", "success")
        return redirect(url_for("customers.detail", customer_id=customer.id))

    return render_template("customers/form.html", customer=customer,
                           form=customer.__dict__)


@bp.route("/<int:customer_id>/years", methods=["POST"])
@login_required
def add_year(customer_id):
    customer = db.session.get(Customer, customer_id) or abort(404)
    year_label = (request.form.get("year_label") or "").strip()

    if not year_label:
        flash("Enter a year label, e.g. FY2025.", "error")
        return redirect(url_for("customers.detail", customer_id=customer.id))

    if FinancialYear.query.filter_by(customer_id=customer.id,
                                     year_label=year_label).first():
        flash(f"{year_label} already exists for this customer.", "error")
        return redirect(url_for("customers.detail", customer_id=customer.id))

    fye_month = customer.financial_year_end_month or 12
    try:
        year_number = int("".join(c for c in year_label if c.isdigit())[:4])
    except ValueError:
        year_number = date.today().year

    end = _month_end(year_number, fye_month)
    start = date(year_number, 1, 1) if fye_month == 12 \
        else date(end.year - 1, end.month, 1)

    # Chain to the prior year so comparatives work.
    previous = (FinancialYear.query
                .filter(FinancialYear.customer_id == customer.id,
                        FinancialYear.end_date < end)
                .order_by(FinancialYear.end_date.desc())
                .first())

    financial_year = FinancialYear(
        customer_id=customer.id, year_label=year_label,
        start_date=start, end_date=end, status="in_progress",
        previous_year_id=previous.id if previous else None)

    db.session.add(financial_year)
    record("financial_year", None, "create", after={"label": year_label})
    db.session.commit()

    flash(f"{year_label} created.", "success")
    return redirect(url_for("customers.workspace",
                            customer_id=customer.id,
                            fy_id=financial_year.id))


def _year_is_deletable(financial_year):
    """Why this year cannot be deleted, or None when it can.

    The firm set the bar: blocked only where a generated document already
    exists against it. Everything else - uploads, a built trial balance,
    mappings - is work in progress, and refusing to delete it would leave a
    mistyped year label stuck on the customer forever.
    """
    if financial_year.report is not None:
        return ("an audit report has been generated against it. Delete the "
                "report first if this year really is a mistake.")
    if financial_year.is_closed:
        return "it is closed. A closed engagement is part of the audit record."
    return None


@bp.route("/<int:customer_id>/fy/<int:fy_id>/delete", methods=["POST"])
@login_required
def delete_year(customer_id, fy_id):
    """Remove a financial year and everything filed under it."""
    customer = db.session.get(Customer, customer_id) or abort(404)
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)
    if financial_year.customer_id != customer.id:
        abort(404)

    blocked = _year_is_deletable(financial_year)
    if blocked:
        flash(f"{financial_year.year_label} cannot be deleted — {blocked}",
              "error")
        return redirect(url_for("customers.detail", customer_id=customer.id))

    # A later year points back at this one for its comparatives. Left alone,
    # the delete would fail on the foreign key; repointed silently, that year
    # would compare itself against a different period without saying so. So
    # the link is cut and the year that loses it is named.
    orphaned = FinancialYear.query.filter_by(previous_year_id=fy_id).all()
    for year in orphaned:
        year.previous_year_id = None

    label = financial_year.year_label
    record("financial_year", fy_id, "delete",
           before={"label": label, "customer": customer.name})
    db.session.delete(financial_year)
    db.session.commit()

    message = f"{label} deleted, with everything filed under it."
    if orphaned:
        names = ", ".join(y.year_label for y in orphaned)
        message += (f" {names} no longer has a prior year linked — its "
                    f"comparatives now need last year's signed accounts.")
    flash(message, "success" if not orphaned else "warning")
    return redirect(url_for("customers.detail", customer_id=customer.id))


@bp.route("/<int:customer_id>/fy/<int:fy_id>/edit", methods=["POST"])
@login_required
def edit_year(customer_id, fy_id):
    """Change a year's period dates, or mark it as the company's first."""
    customer = db.session.get(Customer, customer_id) or abort(404)
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)
    if financial_year.customer_id != customer.id:
        abort(404)

    before = {"start": str(financial_year.start_date),
              "end": str(financial_year.end_date),
              "is_first_year": financial_year.is_first_year}

    start_raw = (request.form.get("start_date") or "").strip()
    end_raw = (request.form.get("end_date") or "").strip()

    try:
        start = date.fromisoformat(start_raw) if start_raw else None
        end = date.fromisoformat(end_raw) if end_raw else None
    except ValueError:
        flash("Enter both dates as YYYY-MM-DD.", "error")
        return redirect(url_for("customers.detail", customer_id=customer.id))

    if start and end and start >= end:
        flash("The period must start before it ends.", "error")
        return redirect(url_for("customers.detail", customer_id=customer.id))

    if start:
        financial_year.start_date = start
    if end:
        financial_year.end_date = end

    # A first year has nothing before it by definition, so any inherited
    # link is wrong and would put a comparative column on accounts that
    # must not have one.
    financial_year.is_first_year = bool(request.form.get("is_first_year"))
    if financial_year.is_first_year:
        financial_year.previous_year_id = None

    record("financial_year", fy_id, "edit", before=before,
           after={"start": str(financial_year.start_date),
                  "end": str(financial_year.end_date),
                  "is_first_year": financial_year.is_first_year})
    db.session.commit()

    flash(f"{financial_year.year_label} updated.", "success")
    return redirect(url_for("customers.detail", customer_id=customer.id))


@bp.route("/<int:customer_id>/fy/<int:fy_id>")
@login_required
def workspace(customer_id, fy_id):
    """The financial-year workspace: Documents -> Statements -> Report."""
    customer = db.session.get(Customer, customer_id) or abort(404)
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    if financial_year.customer_id != customer.id:
        abort(404)

    documents = (Document.query
                 .filter_by(financial_year_id=fy_id)
                 .order_by(Document.uploaded_at.desc()).all())

    statements = (FinancialStatement.query
                  .filter_by(financial_year_id=fy_id).all())

    verified = sum(1 for d in documents if d.review_status == "verified")

    from ..services import xero as xero_service

    return render_template(
        "customers/workspace.html",
        customer=customer, fy=financial_year,
        documents=documents, statements=statements,
        verified_count=verified,
        # The engagement landing page has to show both routes in, or a
        # connected client looks like one with no data.
        xero_available=xero_service.available(),
        xero_conn=xero_service.get_connection(customer.id),
    )
