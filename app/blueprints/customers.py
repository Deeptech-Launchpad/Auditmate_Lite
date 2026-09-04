"""Customer management and financial-year workspace."""
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)
from flask_login import current_user, login_required
from sqlalchemy import or_

from ..extensions import db
from ..models import (Customer, CustomerDocument, Document, FinancialStatement,
                      FinancialYear)
from ..services import storage
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


def _read_profile_upload(file_storage, customer_id=None):
    """Read an uploaded company profile and return (form_values, meta, error).

    Nothing is saved to the customer here. The values come back to be shown
    in the form, because a profile can be stale, can be the wrong company,
    and can be misread - and none of that is discoverable once the values are
    already in the database.
    """
    from ..services.extraction.ai import extract_company_profile
    from ..services import company_profile as profile_service

    if not storage.is_allowed(file_storage.filename or ""):
        return {}, None, "That file type cannot be read."

    try:
        meta = storage.save_company_upload(file_storage, customer_id)
    except ValueError as exc:
        return {}, None, str(exc)

    # A PDF or an image goes to the model as-is; anything else has no page to
    # look at, so its text is pulled out first. Without this a Word profile
    # reached the model with nothing attached and came back empty.
    path = Path(meta["storage_path"])
    file_type = meta["file_type"] or "pdf"
    raw_text = ""
    if file_type not in ("pdf", "image"):
        from ..services.extraction.parsers import run_rule_based
        try:
            parsed = run_rule_based(path, file_type)
            raw_text = parsed.raw_text or ""
            if not raw_text and parsed.rows:
                raw_text = "\n".join(
                    " ".join(str(v) for v in (row.raw_values or [row.label]))
                    for row in parsed.rows)
        except Exception:                          # noqa: BLE001
            raw_text = ""
        if not raw_text:
            try:
                raw_text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                raw_text = ""

    outcome = extract_company_profile(path, file_type, raw_text=raw_text)
    meta["extracted"] = outcome.get("profile") or {}
    meta["extraction_error"] = outcome.get("error")

    if not outcome.get("ok"):
        return {}, meta, outcome.get("error") or "Nothing could be read from it."

    return profile_service.to_form(outcome["profile"]), meta, None


@bp.route("/read-profile", methods=["POST"])
@login_required
def read_profile():
    """Upload a company profile and come back with the form filled in."""
    customer_id = request.form.get("customer_id", type=int)
    customer = db.session.get(Customer, customer_id) if customer_id else None

    file_storage = request.files.get("profile")
    if not file_storage or not file_storage.filename:
        flash("Choose a Business Profile or a set of signed accounts first.",
              "error")
        return redirect(request.referrer or url_for("customers.create"))

    values, meta, error = _read_profile_upload(
        file_storage, customer.id if customer else None)

    if error:
        flash(f"Could not fill the form from that file — {error} "
              f"Enter the details by hand.", "warning")
        return render_template("customers/form.html", customer=customer,
                               form=(customer.__dict__ if customer
                                     else request.form))

    # Kept as evidence, and as the answer to "where did this come from?".
    document = CustomerDocument(
        customer_id=customer.id if customer else None,
        kind="acra_profile",
        original_filename=meta["original_filename"],
        stored_filename=meta["stored_filename"],
        storage_path=meta["storage_path"],
        file_type=meta["file_type"],
        size_bytes=meta["size_bytes"],
        sha256=meta["sha256"],
        extracted=meta["extracted"],
        uploaded_by=current_user.id,
    )
    if customer is not None:
        db.session.add(document)
        db.session.commit()
        pending_path = None
    else:
        # No customer to attach it to yet; carried through the form and
        # adopted when the customer is saved.
        pending_path = meta["storage_path"]

    filled = len(values)
    flash(f"Read {filled} field(s) from {meta['original_filename']}. Check "
          f"every one before saving — a profile can be out of date, and "
          f"{', '.join(profile_not_supplied()).lower()} are not on it.",
          "success")

    # What the profile said wins on the screen, but the preparer's own
    # entries survive: a field they already typed is not overwritten.
    form = dict(customer.__dict__) if customer else {}
    form.update({k: v for k, v in values.items() if v not in (None, "")})

    return render_template("customers/form.html", customer=customer,
                           form=form, pending_profile=pending_path,
                           profile_filled=sorted(values.keys()))


def profile_not_supplied():
    from ..services.company_profile import NOT_IN_PROFILE
    return NOT_IN_PROFILE


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
            financial_year_end_day=request.form.get(
                "financial_year_end_day", type=int),
            is_exempt_private=bool(request.form.get("is_exempt_private")),
            principal_activities=(request.form.get("principal_activities") or "").strip() or None,
            ssic_code=(request.form.get("ssic_code") or "").strip() or None,
            ssic_description=(request.form.get("ssic_description") or "").strip() or None,
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

        # The profile that filled this form was uploaded before there was a
        # customer to file it against. Now there is one.
        pending = (request.form.get("pending_profile") or "").strip()
        if pending:
            moved = storage.adopt_company_upload(pending, customer.id)
            db.session.add(CustomerDocument(
                customer_id=customer.id, kind="acra_profile",
                original_filename=Path(moved).name.split("__", 1)[-1],
                stored_filename=Path(moved).name, storage_path=moved,
                file_type=Path(moved).suffix.lstrip(".").lower() or None,
                uploaded_by=current_user.id))

        # Create the first financial year alongside the customer, so the
        # workspace is immediately usable.
        year_number_raw = (request.form.get("year_number") or "").strip()
        if year_number_raw.isdigit():
            year_number = int(year_number_raw)
            year_label = f"FY{year_number}"
            fye_month = customer.financial_year_end_month or 12

            end = _month_end(year_number, fye_month)
            start = date(end.year - 1, end.month, 1) if fye_month != 12 \
                else date(year_number, 1, 1)

            db.session.add(FinancialYear(
                customer_id=customer.id, year_label=year_label,
                start_date=start, end_date=end, status="in_progress",
                is_first_year=True))

        record("customer", customer.id, "create", after={"name": customer.name})
        db.session.commit()

        flash(f"Customer “{customer.name}” created.", "success")
        return redirect(url_for("customers.detail", customer_id=customer.id))

    return render_template("customers/form.html", customer=None,
                           form={}, default_year_number=str(date.today().year))


@bp.route("/<int:customer_id>")
@login_required
def detail(customer_id):
    customer = db.session.get(Customer, customer_id) or abort(404)

    # A sensible starting label for "Add a financial year" below, so a
    # second and third year need no typing at all - only a first year that
    # does not follow the calendar needs the label changed. One past the
    # latest year already on this customer, or the current calendar year
    # when there is none yet.
    latest = max((y.year_label for y in customer.financial_years
                 if y.year_label[2:6].isdigit()), default=None)
    next_number = str(int(latest[2:6]) + 1) if latest else str(date.today().year)

    return render_template("customers/detail.html", customer=customer,
                           next_year_number=next_number)


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
        customer.financial_year_end_day = request.form.get(
            "financial_year_end_day", type=int)
        customer.is_exempt_private = bool(request.form.get("is_exempt_private"))
        customer.principal_activities = (request.form.get("principal_activities") or "").strip() or None
        customer.ssic_code = (request.form.get("ssic_code") or "").strip() or None
        customer.ssic_description = (request.form.get("ssic_description") or "").strip() or None
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
    """Create a financial year with the period dates typed at intake.

    Dates used to be derived from the customer's FYE month alone, which is
    wrong for exactly the year that most needs to be right: a company's
    first period is never twelve months ending on the usual date, and a
    changed year end produces a long or short year too - see edit_year,
    which already asks for these two dates rather than assuming them. This
    asks the same question at creation instead of creating a wrong period
    and relying on someone to notice and fix it afterward.
    """
    customer = db.session.get(Customer, customer_id) or abort(404)
    year_number = (request.form.get("year_number") or "").strip()

    if not year_number.isdigit():
        flash("Enter the year as a number, e.g. 2025.", "error")
        return redirect(url_for("customers.detail", customer_id=customer.id))
    year_label = f"FY{year_number}"

    if FinancialYear.query.filter_by(customer_id=customer.id,
                                     year_label=year_label).first():
        flash(f"{year_label} already exists for this customer.", "error")
        return redirect(url_for("customers.detail", customer_id=customer.id))

    start_raw = (request.form.get("start_date") or "").strip()
    end_raw = (request.form.get("end_date") or "").strip()
    try:
        start = date.fromisoformat(start_raw) if start_raw else None
        end = date.fromisoformat(end_raw) if end_raw else None
    except ValueError:
        start = end = None

    if not start or not end:
        flash("Enter both the start and end date for the period.", "error")
        return redirect(url_for("customers.detail", customer_id=customer.id))
    if start >= end:
        flash("The period must start before it ends.", "error")
        return redirect(url_for("customers.detail", customer_id=customer.id))

    is_first_year = bool(request.form.get("is_first_year"))

    # A company has exactly one first financial year. The form already
    # hides this checkbox once one is set, so reaching here means either a
    # stale page or a direct post - fall back to "not first year" rather
    # than create a second one.
    existing_first = FinancialYear.query.filter_by(
        customer_id=customer.id, is_first_year=True).first()
    if is_first_year and existing_first:
        flash(f"{existing_first.year_label} is already marked as the first "
              f"financial year. Untick it there before setting another.",
              "error")
        is_first_year = False

    # A first year has nothing before it by definition - see edit_year for
    # the same rule applied to a year that already exists.
    previous = None
    if not is_first_year:
        previous = (FinancialYear.query
                    .filter(FinancialYear.customer_id == customer.id,
                            FinancialYear.end_date < end)
                    .order_by(FinancialYear.end_date.desc())
                    .first())

    financial_year = FinancialYear(
        customer_id=customer.id, year_label=year_label,
        start_date=start, end_date=end, status="in_progress",
        is_first_year=is_first_year,
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

    wants_first = bool(request.form.get("is_first_year"))

    # A company has exactly one first financial year. The form already
    # hides this checkbox on every other year once one is set, so reaching
    # here for a different year means a stale page or a direct post.
    if wants_first:
        existing_first = FinancialYear.query.filter(
            FinancialYear.customer_id == customer.id,
            FinancialYear.is_first_year.is_(True),
            FinancialYear.id != financial_year.id).first()
        if existing_first:
            flash(f"{existing_first.year_label} is already marked as the "
                  f"first financial year. Untick it there before setting "
                  f"another.", "error")
            wants_first = False

    # A first year has nothing before it by definition, so any inherited
    # link is wrong and would put a comparative column on accounts that
    # must not have one.
    financial_year.is_first_year = wants_first
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
