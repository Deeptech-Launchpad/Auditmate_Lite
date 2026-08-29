"""A two-year engagement that exercises the preparation build.

The existing demo client cannot show any of it. Marina Bay has one year, no
prior engagement, no evidence documents, and a chart of accounts every rule
matches - so the mapping screen says "suggested" on all 31 lines, the
outward checks have nothing to check against, and the opening-balance panel
correctly reports that it cannot compare. Everything works and nothing is
visible.

So this seeds a second client with the shape a real engagement has:

    a prior year, approved, with closing balances
    signed accounts stating what was REPORTED for that year
    a current year whose books DISAGREE with the signed accounts on one line
    evidence documents - bank statement, aged receivables, aged payables -
      one of which does not agree
    an account named Exp-7, which no rule will ever place
    an account that has not moved in twelve months

Every figure is deliberate. The disagreements are the point: a seed where
everything ties proves only that the page renders.
"""
import logging
from datetime import date, datetime
from decimal import Decimal

from ..extensions import db
from ..models import (Customer, Document, ExtractedLineItem, FinancialYear,
                      TrialBalanceAccount)

log = logging.getLogger(__name__)

CUSTOMER_NAME = "Sunrise Marine Pte Ltd"

# Last year, as the books hold it. A real year-end trial balance carries the
# profit and loss accounts too, and retained earnings at its OPENING value -
# the year's result has not been appropriated yet. That is what makes the
# comparison against a signed balance sheet non-trivial:
#
#   opening retained earnings        39,000
#   plus the year's result           74,000   (520,000 less 446,000)
#   = closing, as signed            113,000
#
# Balanced: 711,000 each way.
PRIOR_TB = [
    ("1010", "Cash at Bank",        "cash_and_equivalents", "120000.00", None),
    ("1200", "Trade Debtors",       "trade_receivables",     "85000.00", None),
    ("1500", "Plant and Equipment", "ppe",                   "60000.00", None),
    ("5010", "Cost of Sales",       "purchases",            "320000.00", None),
    ("6010", "Salaries & Wages",    "staff_salaries",       "120000.00", None),
    ("6020", "Depreciation",        "depreciation",           "5000.00", None),
    ("6030", "Bank Charges",        "bank_charges",           "1000.00", None),
    ("2010", "Trade Creditors",     "trade_payables",    None, "40000.00"),
    ("2020", "Accruals",            "accruals",          None, "12000.00"),
    ("3010", "Share Capital",       "share_capital",     None, "100000.00"),
    ("3020", "Retained Earnings",   "retained_earnings", None, "39000.00"),
    ("4010", "Sales",               "revenue",           None, "520000.00"),
]

# Last year, as it was SIGNED AND FILED. Identical but for accruals: the
# signed accounts say 15,000, the books now say 12,000. Somebody posted into
# a year that was already closed, and nothing inside this year's trial
# balance would ever show it - it balances perfectly either way.
SIGNED_ACCOUNTS = [
    ("Cash at Bank",        "120000.00"),
    ("Trade Debtors",        "85000.00"),
    ("Plant and Equipment",  "60000.00"),
    ("Trade Creditors",      "40000.00"),
    ("Accruals",             "15000.00"),
    ("Share Capital",       "100000.00"),
    ("Retained Earnings",   "113000.00"),
]

# This year. Balanced: 934,220.00 each way.
CURRENT_TB = [
    ("1010", "Cash at Bank",        "cash_and_equivalents", "163753.90", None),
    ("1200", "Trade Debtors",       "trade_receivables",    "180432.00", None),
    ("1500", "Plant and Equipment", "ppe",                   "55000.00", None),
    ("5010", "Cost of Sales",       "purchases",            "380000.00", None),
    ("6010", "Salaries & Wages",    "staff_salaries",       "140000.00", None),
    ("6020", "Depreciation",        "depreciation",           "5000.00", None),
    ("6030", "Bank Charges",        "bank_charges",           "1200.00", None),
    # No rule will place this, and there is no prior year to carry it from.
    # It is what the mapping screen exists for.
    ("Exp-7", "Exp-7",               None,                    "8834.10", None),
    ("2010", "Trade Creditors",     "trade_payables",     None, "89220.00"),
    # Identical to last year, to the cent. Twelve months and not one entry.
    ("2020", "Accruals",            "accruals",           None, "12000.00"),
    ("3010", "Share Capital",       "share_capital",      None, "100000.00"),
    ("3020", "Retained Earnings",   "retained_earnings",  None, "113000.00"),
    ("4010", "Sales",               "revenue",            None, "620000.00"),
]

# Evidence. The bank and the payables listing agree; the receivables listing
# does not, by 9,000 - which is the kind of difference an auditor is meant to
# go and ask about.
EVIDENCE = [
    ("bank_statement", "DBS Current Account - Dec 2025.pdf", "Bank statement",
     [("Closing balance", "163753.90")]),
    ("receivables", "Aged Receivables 31 Dec 2025.xlsx", "Aged receivables",
     [("Pacific Shipping Pte Ltd", "92000.00"),
      ("Keppel Yards Pte Ltd", "61432.00"),
      ("Harbour Logistics Pte Ltd", "36000.00"),
      ("Total", "189432.00")]),
    ("payables", "Aged Payables 31 Dec 2025.xlsx", "Aged payables",
     [("Sembawang Steel Supply", "54220.00"),
      ("Jurong Marine Parts", "35000.00"),
      ("Total", "89220.00")]),
]


def _document(financial_year, category, filename, user_id, rows,
              verified=True):
    """One evidence document with its figures already extracted."""
    document = Document(
        financial_year_id=financial_year.id,
        original_filename=filename,
        stored_filename=f"seed__{category}__{financial_year.id}",
        # Named honestly. These are database rows with no file behind them,
        # and View or Analyse will say so rather than appearing to be broken.
        storage_path="(seeded for testing - no file on disk)",
        file_type="xlsx" if filename.endswith("xlsx") else "pdf",
        mime_type="application/octet-stream",
        size_bytes=0,
        category=category,
        category_source="manual",
        extraction_status="extracted",
        extraction_engine="seed",
        extraction_confidence=1.0,
        ai_used=False,
        review_status="verified" if verified else "in_review",
        uploaded_by=user_id,
        reviewed_by=user_id if verified else None,
        reviewed_at=datetime.utcnow() if verified else None,
    )
    db.session.add(document)
    db.session.flush()

    for index, (label, amount) in enumerate(rows):
        db.session.add(ExtractedLineItem(
            document_id=document.id, row_index=index,
            raw_label=label, label=label,
            amount=Decimal(amount), confidence=1.0,
            needs_review=False, status="auto"))
    return document


def seed(user_id=None):
    """Create the engagement. Returns (customer, prior_fy, current_fy)."""
    existing = Customer.query.filter_by(name=CUSTOMER_NAME).first()
    if existing is not None:
        raise ValueError(
            f"{CUSTOMER_NAME} already exists (customer {existing.id}). "
            f"Delete it first, or use the engagement that is already there.")

    customer = Customer(
        name=CUSTOMER_NAME,
        legal_name=CUSTOMER_NAME,
        uen="201534567M",
        entity_type="private_limited",
        incorporation_date=date(2015, 4, 14),
        financial_year_end_month=12,
        email="finance@sunrisemarine.example",
        contact_person="Ms Lim Hui Ying",
        # Filled in so the corporate information page and the directors'
        # statement have what they need - two of the readiness checks.
        directors="Mr Rajan Kumar\nMs Lim Hui Ying",
        company_secretary="Ms Serene Ong",
        address_line1="7 Tuas Basin Link",
        address_line2="#04-11",
        postal_code="638774",
        country="Singapore",
        books_currency="SGD",
        created_by=user_id,
    )
    db.session.add(customer)
    db.session.flush()

    prior = FinancialYear(
        customer_id=customer.id, year_label="FY2024",
        start_date=date(2024, 1, 1), end_date=date(2024, 12, 31),
        status="closed",
        # Approved, because a prior year that was never signed off is not a
        # prior year - it is an unfinished engagement.
        tb_approved_at=datetime.utcnow(), tb_approved_by_name="Jey")
    db.session.add(prior)
    db.session.flush()

    current = FinancialYear(
        customer_id=customer.id, year_label="FY2025",
        start_date=date(2025, 1, 1), end_date=date(2025, 12, 31),
        status="in_progress", previous_year_id=prior.id)
    db.session.add(current)
    db.session.flush()

    for financial_year, accounts in ((prior, PRIOR_TB), (current, CURRENT_TB)):
        for code, name, key, debit, credit in accounts:
            db.session.add(TrialBalanceAccount(
                financial_year_id=financial_year.id,
                account_code=code, account_name=name,
                standard_key=key,
                statement_type=None,
                debit=Decimal(debit) if debit else Decimal("0.00"),
                credit=Decimal(credit) if credit else Decimal("0.00"),
                source="upload", confidence=1.0,
                needs_review=key is None))

    _document(current, "signed_accounts",
              "Sunrise Marine - Signed Accounts FY2024.pdf", user_id,
              SIGNED_ACCOUNTS)

    for category, filename, _label, rows in EVIDENCE:
        _document(current, category, filename, user_id, rows)

    db.session.commit()
    log.info("Seeded %s: prior FY %s, current FY %s",
             CUSTOMER_NAME, prior.id, current.id)
    return customer, prior, current


def remove():
    """Delete the seeded engagement and everything under it."""
    customer = Customer.query.filter_by(name=CUSTOMER_NAME).first()
    if customer is None:
        return False

    years = FinancialYear.query.filter_by(customer_id=customer.id).all()
    for financial_year in years:
        documents = Document.query.filter_by(
            financial_year_id=financial_year.id).all()
        # Trial balance rows point at the document they came from; cut the
        # link before deleting or the foreign key refuses.
        (TrialBalanceAccount.query
         .filter_by(financial_year_id=financial_year.id)
         .update({"source_document_id": None}, synchronize_session=False))
        db.session.flush()
        for document in documents:
            ExtractedLineItem.query.filter_by(
                document_id=document.id).delete(synchronize_session=False)
            db.session.delete(document)
        TrialBalanceAccount.query.filter_by(
            financial_year_id=financial_year.id).delete(
                synchronize_session=False)

    # Break the previous_year_id link before deleting, or the row being
    # pointed at cannot go.
    for financial_year in years:
        financial_year.previous_year_id = None
    db.session.flush()
    for financial_year in years:
        db.session.delete(financial_year)
    db.session.delete(customer)
    db.session.commit()
    return True
