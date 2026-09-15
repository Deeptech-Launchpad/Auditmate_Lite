"""Load a real client's figures as a test engagement - without the AI.

The notes library's acceptance test is a real company's signed accounts.
Testing against it means putting that company's trial balance into
AuditMate, and the two routes the product already offers are both wrong for
it.

The upload route runs a document through extraction, and extraction calls
the AI as a fallback whenever its own reader looks unsure. With a hosted
model configured that sends a real client's figures and names to a third
party, which the firm has ruled out until the model runs on its own server.

`seed-beta` avoids the AI, but it holds its figures in source code, and its
company is invented. A real client's accounts do not belong in a git
repository.

So this reads the engagement from a JSON file the caller supplies - kept
under `instance/`, which is git-ignored - and builds it the way `seed-beta`
does: source document, extracted rows, trial balance accounts, all marked
`ai_used=False`. Accounts are mapped by AuditMate's own rules through
`mapping.match_label`, which never calls the AI; anything the rules cannot
place is left for the mapping screen, as it would be for a real upload.

Nothing here knows about any particular client. The file says what to load.
"""
import json
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ..extensions import db
from .audit import record
from ..models import (AccountMapping, Connection, Customer, Document,
                      ExtractedLineItem, FinancialYear, NoteLibraryVersion,
                      TrialBalanceAccount)

log = logging.getLogger(__name__)

# Written into the customer's notes. It is how --replace knows a customer is
# one this loader created, and so safe to delete; a real client that happens
# to share a name carries no such marker and is refused.
MARKER = "Loaded by `flask load-test-engagement` - test data, not a live engagement."

CUSTOMER_FIELDS = (
    "name", "legal_name", "uen", "entity_type", "incorporation_date",
    "financial_year_end_month", "financial_year_end_day",
    "principal_activities", "directors", "company_secretary",
    "address_line1", "address_line2", "postal_code", "country",
    "books_currency", "email", "phone", "contact_person",
)


def _money(value, where):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError(f"{where}: '{value}' is not an amount")


def _day(value, where):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValueError(f"{where}: '{value}' is not a date (use YYYY-MM-DD)")


def read(path):
    """Parse and validate an engagement file. Raises ValueError on any problem.

    Validation is strict on purpose. A trial balance that does not balance is
    refused rather than loaded, because every figure downstream - statements,
    notes, the acceptance test itself - would inherit the difference, and a
    test run against unbalanced books proves nothing.
    """
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    customer = data.get("customer") or {}
    if not customer.get("name"):
        raise ValueError("customer.name is required")

    years = data.get("years") or []
    if not years:
        raise ValueError("at least one year is required")

    labels = [y.get("year_label") for y in years]
    for year in years:
        label = year.get("year_label") or "(unnamed year)"
        year["_start"] = _day(year.get("start"), f"{label} start")
        year["_end"] = _day(year.get("end"), f"{label} end")
        if year.get("previous") and year["previous"] not in labels:
            raise ValueError(f"{label}: previous year '{year['previous']}' "
                             f"is not in the file")

        debit = credit = Decimal("0.00")
        codes = []
        for index, account in enumerate(year.get("accounts") or []):
            where = f"{label} account {index + 1}"
            if not account.get("name"):
                raise ValueError(f"{where}: name is required")
            account["_debit"] = _money(account.get("debit"), where)
            account["_credit"] = _money(account.get("credit"), where)
            if account["_debit"] and account["_credit"]:
                raise ValueError(f"{where} ({account['name']}): both a debit "
                                 f"and a credit - give the net on one side")
            debit += account["_debit"] or 0
            credit += account["_credit"] or 0
            codes.append(account.get("code"))
        if not year.get("accounts"):
            raise ValueError(f"{label}: no accounts")
        repeated = sorted({c for c in codes if c and codes.count(c) > 1})
        if repeated:
            raise ValueError(f"{label}: account codes used twice: "
                             + ", ".join(repeated))
        if debit != credit:
            raise ValueError(
                f"{label} does not balance: debits {debit:,.2f}, credits "
                f"{credit:,.2f}, difference {debit - credit:,.2f}. Refusing "
                f"to load books that do not balance.")
        year["_totals"] = (debit, credit)

    return data


def _remove(customer):
    """Delete a loader-created customer and everything under it."""
    # Mappings and connections belong to the client, not the year, and hold
    # a foreign key the customer delete would otherwise trip over.
    AccountMapping.query.filter_by(customer_id=customer.id).delete(
        synchronize_session=False)
    Connection.query.filter_by(customer_id=customer.id).delete(
        synchronize_session=False)
    for year in customer.financial_years:
        year.previous_year_id = None
    db.session.flush()
    db.session.delete(customer)
    db.session.flush()


def load(path, replace=False, user_id=None):
    """Create the engagement described by the file. Returns a report dict."""
    from . import note_library
    from .mapping import match_label

    data = read(path)
    spec = data["customer"]

    existing = Customer.query.filter_by(name=spec["name"]).first()
    if existing is not None:
        if not replace:
            raise ValueError(
                f"'{spec['name']}' already exists (customer {existing.id}). "
                f"Use --replace to rebuild it from the file.")
        if MARKER not in (existing.notes or ""):
            raise ValueError(
                f"'{spec['name']}' exists but was not created by this loader. "
                f"Refusing to delete what may be a real engagement.")
        _remove(existing)

    customer = Customer(created_by=user_id, notes=MARKER)
    for field in CUSTOMER_FIELDS:
        if field in spec:
            value = spec[field]
            if field == "incorporation_date" and value:
                value = _day(value, "customer.incorporation_date")
            setattr(customer, field, value)
    if not customer.legal_name:
        customer.legal_name = customer.name
    db.session.add(customer)
    db.session.flush()

    by_label, report_years = {}, []
    for year in data["years"]:
        financial_year = FinancialYear(
            customer_id=customer.id,
            year_label=year["year_label"],
            start_date=year["_start"], end_date=year["_end"],
            status=year.get("status") or "in_progress",
            previous_year_id=(by_label[year["previous"]].id
                              if year.get("previous") else None))
        if year.get("approved"):
            financial_year.tb_approved_at = datetime.utcnow()
            financial_year.tb_approved_by_name = "Test data"
        db.session.add(financial_year)
        db.session.flush()
        by_label[year["year_label"]] = financial_year

        # The document the figures came from. Named for the real file, with
        # no file behind it - View and Analyse will say so rather than
        # appearing broken, the convention seed-beta established.
        source = Document(
            financial_year_id=financial_year.id,
            original_filename=year.get("source_filename") or "Trial balance",
            stored_filename=f"test__trial_balance__{financial_year.id}",
            storage_path="(loaded from a test engagement file - no file on disk)",
            file_type="xlsx", mime_type="application/octet-stream",
            size_bytes=0, category="trial_balance", category_source="manual",
            extraction_status="extracted", extraction_engine="test-data",
            extraction_confidence=1.0, ai_used=False,
            review_status="verified", uploaded_by=user_id,
            reviewed_by=user_id, reviewed_at=datetime.utcnow())
        db.session.add(source)
        db.session.flush()

        unmapped, from_file = [], 0
        for index, account in enumerate(year["accounts"]):
            db.session.add(ExtractedLineItem(
                document_id=source.id, row_index=index,
                account_code=account.get("code"),
                account_type=account.get("type"),
                raw_label=account["name"], label=account["name"],
                debit=account["_debit"], credit=account["_credit"],
                confidence=1.0, needs_review=False, status="auto"))

            # A settled year states its mapping; an open year is left to the
            # rules, exactly as a real upload is. The difference matters on
            # the mapping screen: an account whose mapping agrees with last
            # year's is shown as "Carried from last year" - settled, nothing
            # to check. If a closed prior year were mapped by the same rule
            # guesses, every wrong guess would be repeated in the open year
            # and presented there as already decided.
            if account.get("standard_key"):
                key = account["standard_key"]
                from_file += 1
            else:
                rule = match_label(account["name"], customer.id,
                                   account_type=account.get("type"))
                key = rule["line_key"] if rule else None
            if key is None:
                unmapped.append(account["name"])
            db.session.add(TrialBalanceAccount(
                financial_year_id=financial_year.id,
                account_code=account.get("code"),
                account_name=account["name"],
                account_type=account.get("type"),
                standard_key=key, statement_type=None,
                debit=account["_debit"] or Decimal("0.00"),
                credit=account["_credit"] or Decimal("0.00"),
                source="upload", source_document_id=source.id,
                confidence=1.0 if key else 0.0,
                needs_review=key is None))

        # A deliberate exception to the library's own version rule, stated in
        # the file and recorded, never inferred. The client's acceptance-test
        # year can fall outside the version it is meant to test: library 2.1
        # is valid for year ends from 1 January 2024, and the test year ends
        # 31 December 2023. pin_version() rightly refuses that. Pinning it
        # anyway is a test decision someone made, so it carries its reason
        # into the audit trail, and `flask note-library` lists it.
        exception = None
        if year.get("library_version"):
            version = NoteLibraryVersion.query.filter_by(
                version_label=str(year["library_version"])).first()
            if version is None:
                raise ValueError(f"{year['year_label']}: library version "
                                 f"{year['library_version']} is not loaded")
            if not year.get("library_exception"):
                raise ValueError(
                    f"{year['year_label']}: pinning library "
                    f"{version.version_label} by hand needs a "
                    f"library_exception saying why")
            financial_year.library_version_id = version.id
            exception = year["library_exception"]
            record("financial_year", financial_year.id, "library_exception",
                   after={"library_version": version.version_label,
                          "version_status": version.status,
                          "valid_from": version.valid_from.isoformat(),
                          "valid_to": version.valid_to.isoformat(),
                          "year_end": financial_year.end_date.isoformat(),
                          "reason": exception})
        note_library.pin_version(financial_year)
        report_years.append({
            "label": financial_year.year_label,
            "id": financial_year.id,
            "accounts": len(year["accounts"]),
            "debit": year["_totals"][0], "credit": year["_totals"][1],
            "approved": bool(year.get("approved")),
            "unmapped": unmapped,
            "from_file": from_file,
            "library_exception": exception,
            "library": (db.session.get(NoteLibraryVersion,
                                       financial_year.library_version_id).version_label
                        if financial_year.library_version_id else None),
        })

    db.session.flush()
    # Finer categories after every year exists, oldest first, so an open year
    # can carry a category its prior year settled.
    from . import line_codes
    for entry in report_years:
        summary = line_codes.assign_year(
            db.session.get(FinancialYear, entry["id"]), commit=False)
        entry["line_codes"] = summary

    db.session.commit()
    log.info("Loaded test engagement %s (customer %s)", customer.name, customer.id)
    return {"customer": customer.name, "customer_id": customer.id,
            "years": report_years,
            "known_disagreements": data.get("_known_disagreements") or []}
