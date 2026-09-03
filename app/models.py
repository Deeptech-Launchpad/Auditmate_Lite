"""Database models for Auditmate Lite.

The whole schema lives in one module because the aggregates are tightly
related and it keeps imports simple. Roughly it follows the pipeline:

    Customer -> FinancialYear -> Document -> ExtractedLineItem
                              -> FinancialStatement -> StatementLine
                              -> AuditReport -> AuditReportSection

Every money-bearing row keeps a pointer back to where the number came from,
because this is audit software and provenance is the point.
"""
from datetime import datetime, date

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from flask_login import UserMixin
from sqlalchemy import JSON, Numeric

from .extensions import db, login_manager

_hasher = PasswordHasher()


# --------------------------------------------------------------------------
# Reference vocabularies (kept as plain tuples, not DB enums, so adding a
# value later is a code change rather than a migration).
# --------------------------------------------------------------------------

ENTITY_TYPES = [
    ("private_limited", "Private Limited Company (Pte Ltd)"),
    ("public_limited", "Public Limited Company"),
    ("sole_proprietorship", "Sole Proprietorship"),
    ("partnership", "Partnership"),
    ("llp", "Limited Liability Partnership (LLP)"),
    ("branch", "Branch of Foreign Company"),
    ("other", "Other"),
]

DOCUMENT_CATEGORIES = [
    ("trial_balance", "Trial Balance"),
    ("balance_sheet", "Balance Sheet"),
    ("profit_and_loss", "Profit & Loss / Income Statement"),
    # One of the four primary statements. Not in TB_SOURCE_PRECEDENCE: it is
    # derived from the others and carries movements, not a list of balances,
    # so it can never build the accounts.
    ("cash_flow", "Cash Flow Statement"),
    ("general_ledger", "General Ledger"),
    # Last year's finished accounts. Not evidence like the others - it is
    # also required DATA: the prior-year column of every statement comes
    # from it, and so does last year's mapping.
    ("signed_accounts", "Signed Accounts (prior year)"),
    # Xero's trial balance at LAST year's year end. Never builds this year's
    # accounts - it is not in TB_SOURCE_PRECEDENCE - and is never compared
    # line by line against them either, because it describes a different
    # year. It exists for the comparative column and for the opening-balance
    # check against what was signed.
    ("prior_trial_balance", "Trial Balance (prior year)"),
    ("prior_cash_flow", "Cash Flow Statement (prior year)"),
    ("bank_statement", "Bank Statement"),
    ("vendor_invoice", "Vendor Invoice"),
    ("customer_invoice", "Customer Invoice"),
    ("salary_schedule", "Salary Schedule"),
    ("payables", "Accounts Payable Listing"),
    ("receivables", "Accounts Receivable Listing"),
    ("fixed_asset_register", "Fixed Asset Register"),
    # Named for what the firm actually files here. The GST balance is checked
    # against a return filed under this category (see readiness.py), so the
    # label has to say so rather than leaving the preparer guessing.
    ("tax_document", "Tax Document / GST Return"),
    ("other", "Other"),
]

# Which documents the accounts are built FROM, best first.
#
# A client sends several documents describing the same year, and they overlap:
# a general ledger, the profit and loss summarising it, and a balance sheet
# summarising it again all state the same money. Adding them together counts
# that money two or three times - one real engagement came out at 21.8m of
# debits against 10.9m of credits on a company turning over about 3.5m.
#
# So exactly one of them builds the accounts: the best that was supplied.
# Everything else is held back as evidence to check the result against, which
# is what a client's own totals are for.
TB_SOURCE_PRECEDENCE = [
    "trial_balance",      # says what every account holds. Nothing beats it.
    "balance_sheet",      # with the P&L, a trial balance split over two pages
    "profit_and_loss",
    "general_ledger",     # last resort. See below.
]

# The general ledger used to rank second, above the balance sheet and the
# profit and loss. It is now last, because it is the worst of the four for
# this job on two counts.
#
# It states MOVEMENTS, not balances. A ledger says what happened during the
# year; a trial balance says what each account holds at the end of it. The
# difference is every account's opening balance, and unless the export
# carries those, every balance sheet account comes out understated - while
# still balancing.
#
# And its rows are named after suppliers, not accounts: "Ang Mo Kio Hardware
# Pte Ltd - invoice 4471", never "Cost of Services". Those names map to
# nothing. On one engagement 2,308 of 2,339 ledger rows matched no account
# at all, and the accounts built from it were hundreds of unmapped supplier
# names.
#
# The balance sheet and the profit and loss are already what a trial balance
# is: one line per account, at the year end, in account names, complete
# between them.

# Balance sheet and profit and loss are two halves of one source: one carries
# the assets, liabilities and equity, the other the income and expenses. Taken
# together they cover every account exactly once, so they are used together or
# not at all.
TB_SOURCE_PAIRED = {"balance_sheet", "profit_and_loss"}

STATEMENT_TYPES = [
    ("trial_balance", "Trial Balance"),
    ("profit_and_loss", "Statement of Comprehensive Income"),
    ("balance_sheet", "Statement of Financial Position"),
    ("changes_in_equity", "Statement of Changes in Equity"),
    ("cash_flow", "Statement of Cash Flows"),
    ("accounts_payable", "Accounts Payable"),
    ("accounts_receivable", "Accounts Receivable"),
]

FY_STATUSES = [
    ("in_progress", "In Progress"),
    ("statements_shared", "Statements Shared"),
    ("approved", "Approved"),
    ("report_generated", "Report Generated"),
    ("closed", "Closed"),
]


def label_for(vocab, key, default="—"):
    """Look up the human label for a stored key."""
    for k, v in vocab:
        if k == key:
            return v
    return default


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="auditor", nullable=False)  # admin | auditor
    is_active_flag = db.Column("is_active", db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_login_at = db.Column(db.DateTime)

    def set_password(self, raw: str) -> None:
        self.password_hash = _hasher.hash(raw)

    def check_password(self, raw: str) -> bool:
        try:
            return _hasher.verify(self.password_hash, raw)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    @property
    def is_active(self) -> bool:          # Flask-Login reads this
        return self.is_active_flag

    @property
    def initials(self) -> str:
        parts = [p for p in (self.name or "").split() if p]
        return "".join(p[0].upper() for p in parts[:2]) or "?"

    def __repr__(self):
        return f"<User {self.email}>"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# --------------------------------------------------------------------------
# Customers and financial years
# --------------------------------------------------------------------------

class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, index=True)
    legal_name = db.Column(db.String(255))
    entity_type = db.Column(db.String(40), default="private_limited")

    # Singapore identifiers (ACRA)
    uen = db.Column(db.String(20), index=True)
    gst_reg_no = db.Column(db.String(30))
    incorporation_date = db.Column(db.Date)
    # Companies choose their own FYE in Singapore, so we store the month.
    financial_year_end_month = db.Column(db.Integer, default=12)
    # And the day, because a year end is not always the month end - ACRA
    # profiles carry dates like 30 June, and a company that changed its year
    # end has a period the month alone cannot describe.
    financial_year_end_day = db.Column(db.Integer)

    # An exempt private company is a private company with at most 20
    # shareholders, none of them corporate. It is not cosmetic: it decides
    # audit exemption and changes what the accounts must disclose, and it is
    # stated on the ACRA profile - so it is captured rather than inferred
    # from entity_type, which cannot express it.
    is_exempt_private = db.Column(db.Boolean, default=False, nullable=False)

    # What the company actually does, in ACRA's own words and codes. Feeds
    # the corporate information note and the revenue wording, which is why
    # the firm asked for it at intake rather than typed again per year.
    principal_activities = db.Column(db.Text)
    ssic_code = db.Column(db.String(20))
    ssic_description = db.Column(db.String(255))

    email = db.Column(db.String(255))
    phone = db.Column(db.String(50))
    contact_person = db.Column(db.String(120))

    # Named on the cover page and in the directors' statement of the annual
    # report. Several directors are separated by a newline.
    directors = db.Column(db.Text)
    company_secretary = db.Column(db.String(200))

    address_line1 = db.Column(db.String(255))
    address_line2 = db.Column(db.String(255))
    postal_code = db.Column(db.String(20))
    country = db.Column(db.String(80), default="Singapore")

    books_currency = db.Column(db.String(3), default="SGD")
    engagement_partner_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    notes = db.Column(db.Text)

    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))

    engagement_partner = db.relationship("User", foreign_keys=[engagement_partner_id])
    company_documents = db.relationship(
        "CustomerDocument", back_populates="customer",
        cascade="all, delete-orphan", order_by="CustomerDocument.uploaded_at.desc()")
    financial_years = db.relationship(
        "FinancialYear", back_populates="customer",
        cascade="all, delete-orphan", order_by="FinancialYear.start_date.desc()",
    )

    @property
    def entity_type_label(self):
        return label_for(ENTITY_TYPES, self.entity_type)

    def __repr__(self):
        return f"<Customer {self.name}>"


class FinancialYear(db.Model):
    __tablename__ = "financial_years"
    __table_args__ = (db.UniqueConstraint("customer_id", "year_label",
                                          name="uq_fy_customer_label"),)

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    year_label = db.Column(db.String(30), nullable=False)      # e.g. "FY2025"
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    status = db.Column(db.String(30), default="in_progress", nullable=False)

    # Links to the prior year so statements can show comparatives.
    previous_year_id = db.Column(db.Integer, db.ForeignKey("financial_years.id"))

    # The company's FIRST financial period since incorporation.
    #
    # Not the same thing as "no previous year linked", which is why it has to
    # be asked rather than inferred. A year with nothing before it in
    # Auditmate is usually just a client the firm has audited for a decade
    # and only now moved onto this tool - that engagement still needs a
    # comparative column and still wants last year's signed accounts. A true
    # first year needs neither, and saying so is a statement about the
    # company, not about what happens to be in our database.
    #
    # Four things follow from it, all of them wrong if guessed:
    # no comparative column, no prior-year documents demanded, comparative
    # note wording for a period that may not be twelve months, and opening
    # balances of nil.
    is_first_year = db.Column(db.Boolean, default=False, nullable=False)

    shared_at = db.Column(db.DateTime)
    approved_at = db.Column(db.DateTime)
    approved_by_name = db.Column(db.String(160))
    approval_note = db.Column(db.Text)

    # Closing is the last act of an engagement: the report has been issued
    # and the file is done. Recorded rather than merely flagged, because an
    # audit file's own history is part of the evidence.
    closed_at = db.Column(db.DateTime)
    closed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    closed_note = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = db.relationship("Customer", back_populates="financial_years")
    previous_year = db.relationship("FinancialYear", remote_side=[id])
    documents = db.relationship("Document", back_populates="financial_year",
                                cascade="all, delete-orphan")
    statements = db.relationship("FinancialStatement", back_populates="financial_year",
                                 cascade="all, delete-orphan")
    reports = db.relationship("AuditReport", back_populates="financial_year",
                              cascade="all, delete-orphan")
    review_links = db.relationship(
        "CustomerReviewLink", back_populates="financial_year",
        cascade="all, delete-orphan",
        order_by="CustomerReviewLink.created_at.desc()")
    tb_versions = db.relationship(
        "TrialBalanceVersion", back_populates="financial_year",
        cascade="all, delete-orphan",
        order_by="TrialBalanceVersion.version_no.desc()")

    tb_accounts = db.relationship(
        "TrialBalanceAccount", back_populates="financial_year",
        cascade="all, delete-orphan",
        order_by="TrialBalanceAccount.account_code",
    )
    # Where the standard trial balance has got to. Statements and the audit
    # report are gated on this reaching "approved".
    tb_status = db.Column(db.String(30), default="draft", nullable=False)
    tb_approved_at = db.Column(db.DateTime)
    tb_approved_by_name = db.Column(db.String(160))

    prior_notes = db.relationship(
        "PriorYearNote", back_populates="financial_year",
        cascade="all, delete-orphan")
    versions = db.relationship(
        "StatementVersion", back_populates="financial_year",
        cascade="all, delete-orphan",
        order_by="StatementVersion.version_no.desc()",
    )

    @property
    def active_review_link(self):
        """The most recent link that can still be opened."""
        for link in self.review_links:
            if link.is_usable:
                return link
        return None

    @property
    def latest_tb_version(self):
        return self.tb_versions[0] if self.tb_versions else None

    @property
    def pending_tb_changes(self):
        """Customer changes still awaiting the auditor's decision."""
        total = 0
        for version in self.tb_versions:
            total += len(version.pending_changes)
        return total

    @property
    def tb_is_approved(self):
        return self.tb_status == "approved"

    @property
    def tb_is_stale(self):
        """True when a source has changed since the trial balance was built.

        Verifying a document, changing which sheets are read, or uploading
        another file all change what the trial balance SHOULD say - but the
        trial balance itself does not move until it is rebuilt. Without this
        the auditor sees old figures with nothing telling them so.

        Only a document that could BUILD the accounts counts. Exactly one
        kind does - see TB_SOURCE_PRECEDENCE and trial_balance.choose_sources
        - and everything else is evidence held against the figures rather
        than a figure. Last year's signed accounts saying something new about
        last year does not make this year's trial balance wrong, and telling
        the preparer to rebuild over it sends them to do nothing.
        """
        if not self.tb_accounts:
            return False
        built = max((a.created_at for a in self.tb_accounts if a.created_at),
                    default=None)
        if built is None:
            return False
        for document in self.documents:
            if document.review_status != "verified":
                continue
            if (document.category not in TB_SOURCE_PRECEDENCE
                    and document.file_type != "xero"):
                continue
            changed = document.reviewed_at or document.uploaded_at
            if changed and changed > built:
                return True
        return False

    @property
    def tb_totals(self):
        """Debit/credit totals and whether the trial balance balances."""
        from decimal import Decimal
        debit = sum((a.debit or 0) for a in self.tb_accounts)
        credit = sum((a.credit or 0) for a in self.tb_accounts)
        difference = Decimal(str(debit)) - Decimal(str(credit))
        return {"debit": debit, "credit": credit, "difference": difference,
                "balanced": abs(difference) < Decimal("0.01"),
                "accounts": len(self.tb_accounts),
                "unmapped": sum(1 for a in self.tb_accounts if not a.standard_key)}

    @property
    def latest_version(self):
        return self.versions[0] if self.versions else None

    @property
    def final_version(self):
        for version in self.versions:
            if version.status == "final":
                return version
        return None

    @property
    def status_label(self):
        return label_for(FY_STATUSES, self.status)

    @property
    def is_approved(self):
        return self.status in ("approved", "report_generated", "closed")

    @property
    def is_closed(self):
        return self.status == "closed"

    @property
    def report(self):
        """The engagement's audit report, if one has been started."""
        return self.reports[0] if self.reports else None

    @property
    def can_close(self):
        """Whether the engagement is in a state that can be signed off.

        The bar is the approved trial balance and an existing report - the
        two things the file cannot be finished without. It is deliberately
        not the full customer-approval chain, because firms differ on
        whether that happens inside the system or by email.
        """
        return bool(self.tb_is_approved and self.reports and not self.is_closed)

    @property
    def documents_pending_review(self):
        return sum(1 for d in self.documents if d.review_status != "verified")

    def __repr__(self):
        return f"<FinancialYear {self.year_label}>"


# --------------------------------------------------------------------------
# Documents and extraction
# --------------------------------------------------------------------------

class Document(db.Model):
    __tablename__ = "documents"

    id = db.Column(db.Integer, primary_key=True)
    financial_year_id = db.Column(db.Integer, db.ForeignKey("financial_years.id"),
                                  nullable=False)

    original_filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)
    storage_path = db.Column(db.String(500), nullable=False)
    file_type = db.Column(db.String(20))         # xlsx | csv | docx | pdf | image
    mime_type = db.Column(db.String(120))
    size_bytes = db.Column(db.BigInteger)
    sha256 = db.Column(db.String(64), index=True)
    category = db.Column(db.String(40), default="other")
    # Where the category came from: filename | content | manual.
    #
    # Recorded because the category is not a label - it decides which
    # document the accounts are built FROM - so the auditor has to be able
    # to see whether a person chose it or the app guessed, and a guess must
    # never overwrite a choice.
    category_source = db.Column(db.String(20))
    category_reason = db.Column(db.String(255))
    page_count = db.Column(db.Integer)

    # queued | processing | extracted | failed
    # Which sheets of a workbook to read. A client's "Management Accounts"
    # file holds the ledger, the schedules and the summary side by side, and
    # only the auditor knows which one is authoritative - so this is a
    # choice, not a guess. Empty means "decide automatically".
    source_sheets = db.Column(JSON)

    extraction_status = db.Column(db.String(20), default="queued", nullable=False)
    extraction_engine = db.Column(db.String(40))   # openpyxl | csv | python-docx | pdfplumber | claude
    extraction_error = db.Column(db.Text)
    extraction_confidence = db.Column(db.Float)
    ai_used = db.Column(db.Boolean, default=False, nullable=False)

    # pending | in_review | verified | rejected
    review_status = db.Column(db.String(20), default="pending", nullable=False)

    uploaded_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)

    financial_year = db.relationship("FinancialYear", back_populates="documents")
    uploader = db.relationship("User", foreign_keys=[uploaded_by])
    line_items = db.relationship(
        "ExtractedLineItem", back_populates="document",
        cascade="all, delete-orphan", order_by="ExtractedLineItem.row_index",
    )

    @property
    def category_label(self):
        return label_for(DOCUMENT_CATEGORIES, self.category)

    @property
    def size_display(self):
        n = self.size_bytes or 0
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    @property
    def rows_needing_review(self):
        return sum(1 for li in self.line_items
                   if li.needs_review and li.status == "auto")

    def __repr__(self):
        return f"<Document {self.original_filename}>"


class ExtractedLineItem(db.Model):
    """One row pulled out of a source document.

    This is the unit the Review & Correct screen edits. `raw_*` fields hold
    what the extractor saw; the plain fields hold the current (possibly
    auditor-corrected) value.
    """
    __tablename__ = "extracted_line_items"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False)
    row_index = db.Column(db.Integer, default=0, nullable=False)

    raw_label = db.Column(db.Text)
    raw_values = db.Column(JSON)

    label = db.Column(db.Text)
    account_code = db.Column(db.String(50))
    account_type = db.Column(db.String(60))
    amount = db.Column(Numeric(18, 2))
    debit = db.Column(Numeric(18, 2))
    credit = db.Column(Numeric(18, 2))
    period = db.Column(db.String(10), default="current")   # current | previous

    confidence = db.Column(db.Float, default=1.0)
    needs_review = db.Column(db.Boolean, default=False, nullable=False)

    # Where this came from: {"sheet": "Sheet1", "cell": "B12"} or {"page": 3}
    source_ref = db.Column(JSON)

    # auto | corrected | accepted | discarded
    status = db.Column(db.String(20), default="auto", nullable=False)
    corrected_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    corrected_at = db.Column(db.DateTime)

    document = db.relationship("Document", back_populates="line_items")

    @property
    def source_display(self):
        ref = self.source_ref or {}
        if "cell" in ref:
            return f"{ref.get('sheet', '')}!{ref['cell']}".lstrip("!")
        if "page" in ref:
            return f"Page {ref['page']}"
        if "row" in ref:
            return f"Row {ref['row']}"
        return "—"

    def __repr__(self):
        return f"<LineItem {self.label!r} {self.amount}>"


# --------------------------------------------------------------------------
# Account mapping (raw label -> statement line)
# --------------------------------------------------------------------------

class AccountMapping(db.Model):
    """Rule that maps an extracted label onto a statement line.

    customer_id NULL means a global seed rule. Customer-specific rules win,
    so a correction made once is remembered for that customer next year.
    """
    __tablename__ = "account_mappings"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"))
    pattern = db.Column(db.String(255), nullable=False)
    match_type = db.Column(db.String(20), default="contains")   # exact | contains | regex
    statement_type = db.Column(db.String(40), nullable=False)
    line_key = db.Column(db.String(80), nullable=False)
    sign = db.Column(db.Integer, default=1)
    priority = db.Column(db.Integer, default=100)
    source = db.Column(db.String(20), default="seed")           # seed | learned | manual
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer")

    def __repr__(self):
        return f"<Mapping {self.pattern!r} -> {self.line_key}>"


# --------------------------------------------------------------------------
# Financial statements
# --------------------------------------------------------------------------

class FinancialStatement(db.Model):
    __tablename__ = "financial_statements"

    id = db.Column(db.Integer, primary_key=True)
    financial_year_id = db.Column(db.Integer, db.ForeignKey("financial_years.id"),
                                  nullable=False)
    statement_type = db.Column(db.String(40), nullable=False)
    status = db.Column(db.String(20), default="draft", nullable=False)  # draft|shared|approved
    version = db.Column(db.Integer, default=1)

    generated_at = db.Column(db.DateTime, default=datetime.utcnow)
    shared_at = db.Column(db.DateTime)
    approved_at = db.Column(db.DateTime)
    notes = db.Column(db.Text)

    financial_year = db.relationship("FinancialYear", back_populates="statements")
    lines = db.relationship(
        "StatementLine", back_populates="statement",
        cascade="all, delete-orphan", order_by="StatementLine.sort_order",
    )

    @property
    def type_label(self):
        return label_for(STATEMENT_TYPES, self.statement_type)

    @property
    def total_current(self):
        return sum((l.effective_amount or 0) for l in self.lines if l.is_total)

    def __repr__(self):
        return f"<Statement {self.statement_type}>"


class StatementLine(db.Model):
    __tablename__ = "statement_lines"

    id = db.Column(db.Integer, primary_key=True)
    statement_id = db.Column(db.Integer, db.ForeignKey("financial_statements.id"),
                             nullable=False)

    line_key = db.Column(db.String(80), nullable=False)
    label = db.Column(db.String(255), nullable=False)
    group_key = db.Column(db.String(80))
    sort_order = db.Column(db.Integer, default=0)
    indent = db.Column(db.Integer, default=0)

    amount_current = db.Column(Numeric(18, 2), default=0)
    amount_previous = db.Column(Numeric(18, 2))

    # The figure that came straight from the mapped documents, before any
    # formula ran. Kept separately so recalculation is idempotent: retained
    # earnings needs its *opening* balance, and reading amount_current would
    # re-add this year's profit on every recompute.
    base_amount = db.Column(Numeric(18, 2), default=0)

    is_subtotal = db.Column(db.Boolean, default=False)
    is_total = db.Column(db.Boolean, default=False)

    # Breakdown line: feeds its group subtotal and shows in the Detailed
    # Profit and Loss Statement, but not on the face of the statutory
    # statement (which presents only the subtotal).
    is_detail = db.Column(db.Boolean, default=False)
    # Note number shown in the statement's "Notes" column.
    note_ref = db.Column(db.String(80))
    is_computed = db.Column(db.Boolean, default=False)
    formula = db.Column(db.String(255))

    source = db.Column(db.String(20), default="auto")   # auto | manual | computed
    manual_override_amount = db.Column(Numeric(18, 2))

    # Wording differs between clients - Revenue or Turnover, Cost of sales or
    # Cost of goods sold. The label is presentation only, so unlike the
    # figure it can be rewritten freely without anything downstream moving.
    label_override = db.Column(db.String(255))

    # Which extracted rows fed this line — the provenance trail.
    source_line_item_ids = db.Column(JSON)

    statement = db.relationship("FinancialStatement", back_populates="lines")

    @property
    def effective_amount(self):
        """The number actually shown: an auditor override wins over the
        auto-calculated figure."""
        if self.manual_override_amount is not None:
            return self.manual_override_amount
        return self.amount_current

    @property
    def is_overridden(self):
        return self.manual_override_amount is not None

    @property
    def effective_label(self):
        """The wording actually printed."""
        return self.label_override or self.label

    @property
    def label_is_overridden(self):
        return bool(self.label_override)

    def __repr__(self):
        return f"<Line {self.line_key} {self.effective_amount}>"


# --------------------------------------------------------------------------
# The Standard Trial Balance
#
# Every input - accounting-software pull, uploaded document, or an auditor's
# own adjustment - normalises into this one table. It is the single source of
# truth for the engagement: statements are built from it, the customer reviews
# it, and nothing downstream is produced until it is approved.
# --------------------------------------------------------------------------

TB_SOURCES = [
    ("xero", "Xero"),
    ("quickbooks", "QuickBooks"),
    ("tally", "Tally"),
    ("upload", "Uploaded document"),
    ("manual", "Entered by auditor"),
    ("adjustment", "Audit adjustment"),
]

# --------------------------------------------------------------------------
# Accounting software connections (Xero today; QuickBooks and Tally later)
# --------------------------------------------------------------------------

PROVIDERS = [
    ("xero", "Xero"),
    ("quickbooks", "QuickBooks Online"),
    ("tally", "Tally"),
]


class Connection(db.Model):
    """An authorised link between one customer and one accounting system.

    Provider-neutral on purpose: QuickBooks needs exactly the same fields
    under different names (realmId rather than tenantId), so Phase D adds an
    adapter rather than a second table.

    Tokens are stored encrypted - see services/secrets.py. Nothing in this
    model returns a usable token; the service layer decrypts on use.
    """
    __tablename__ = "connections"
    __table_args__ = (
        db.UniqueConstraint("customer_id", "provider",
                            name="uq_connection_customer_provider"),
    )

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"),
                            nullable=False, index=True)
    provider = db.Column(db.String(20), nullable=False, default="xero")

    # Which organisation inside the provider. One Xero login can hold many
    # client organisations, so this is what says "these are Marina Bay's
    # books and not another client's".
    tenant_id = db.Column(db.String(100))
    tenant_name = db.Column(db.String(255))

    access_token_enc = db.Column(db.Text)
    refresh_token_enc = db.Column(db.Text)
    # Access tokens last 30 minutes; refresh tokens 60 days and they rotate
    # on every use, so both expiries are tracked.
    access_expires_at = db.Column(db.DateTime)
    refresh_expires_at = db.Column(db.DateTime)

    scopes = db.Column(db.Text)
    status = db.Column(db.String(20), default="connected", nullable=False)
    last_error = db.Column(db.Text)

    last_synced_at = db.Column(db.DateTime)
    last_sync_accounts = db.Column(db.Integer)

    connected_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    connected_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    customer = db.relationship("Customer", backref=db.backref(
        "connections", cascade="all, delete-orphan"))
    user = db.relationship("User")

    @property
    def provider_label(self):
        return label_for(PROVIDERS, self.provider)

    @property
    def is_live(self):
        """Connected, pointed at an organisation, and not expired."""
        if self.status != "connected" or not self.tenant_id:
            return False
        if self.refresh_expires_at and self.refresh_expires_at < datetime.utcnow():
            return False
        return True

    @property
    def needs_attention(self):
        """Something the auditor has to act on before a pull will work."""
        return self.status != "connected" or not self.tenant_id or not self.is_live

    def __repr__(self):
        return f"<Connection {self.provider} {self.tenant_name!r}>"


TB_STATUSES = [
    ("draft", "Draft"),
    ("shared", "Sent to customer"),
    ("customer_submitted", "Customer returned changes"),
    ("approved", "Approved"),
]


class TrialBalanceAccount(db.Model):
    """One account line in the engagement's standard trial balance."""
    __tablename__ = "trial_balance_accounts"

    id = db.Column(db.Integer, primary_key=True)
    financial_year_id = db.Column(db.Integer, db.ForeignKey("financial_years.id"),
                                  nullable=False, index=True)

    account_code = db.Column(db.String(50))
    account_name = db.Column(db.String(255), nullable=False)

    # The CLIENT's own classification of the account - Xero's "Revenue",
    # "Less Operating Expenses", or whatever a workbook's Type column says.
    # Deliberately not ours: standard_key is Auditmate's answer to where the
    # account belongs, and this is the client's. Seeing the two side by side
    # is how a preparer notices that what the books call a liability has been
    # mapped to an expense.
    account_type = db.Column(db.String(60))

    # Auditmate's canonical account identifier. This is the statement line the
    # account rolls up to. Mapping happens HERE, once, rather than separately
    # inside each statement - so correcting an unmapped account fixes every
    # statement that derives from it.
    standard_key = db.Column(db.String(80), index=True)
    statement_type = db.Column(db.String(40))

    debit = db.Column(Numeric(18, 2), default=0)
    credit = db.Column(Numeric(18, 2), default=0)

    source = db.Column(db.String(20), default="upload", nullable=False)
    source_document_id = db.Column(db.Integer, db.ForeignKey("documents.id"))
    source_ref = db.Column(JSON)

    confidence = db.Column(db.Float, default=1.0)
    needs_review = db.Column(db.Boolean, default=False, nullable=False)
    is_adjustment = db.Column(db.Boolean, default=False, nullable=False)

    # True when an auditor set this mapping by hand. A rebuild preserves
    # those and re-derives the rest, so a rule that has since been corrected
    # actually takes effect - previously EVERY mapping was remembered, and a
    # wrong automatic guess stuck to the account forever.
    mapping_is_manual = db.Column(db.Boolean, default=False, nullable=False)

    notes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    financial_year = db.relationship("FinancialYear", back_populates="tb_accounts")
    source_document = db.relationship("Document")

    @property
    def net(self):
        """Debit less credit - the signed balance."""
        return (self.debit or 0) - (self.credit or 0)

    @property
    def source_label(self):
        return label_for(TB_SOURCES, self.source)

    @property
    def source_detail(self):
        """Where this figure came from, specifically enough to go and check.

        "Uploaded document" tells an auditor nothing they can act on; the
        document's own category - Bank Statement, Salary Schedule - tells
        them which file to open.
        """
        if self.source in ("upload", "xero") and self.source_document is not None:
            return self.source_document.category_label
        return None

    @property
    def _classification(self):
        # Imported here rather than at module scope: the service reads the
        # statement templates, which import models.
        from .services.classify import classify
        return classify(self.standard_key)

    @property
    def fs_label(self):
        """Which financial statement this account lands in."""
        found = self._classification
        return found["fs"] if found else None

    @property
    def category_label(self):
        """The heading it sits under in that statement."""
        found = self._classification
        return found["category"] if found else None

    @property
    def is_mapped(self):
        return bool(self.standard_key)

    def __repr__(self):
        return f"<TBAccount {self.account_name!r} {self.net}>"


# --------------------------------------------------------------------------
# Statement versions (the customer review round-trip)
# --------------------------------------------------------------------------

VERSION_STATUSES = [
    ("draft", "Draft"),
    ("sent", "Sent to customer"),
    ("customer_revised", "Customer returned changes"),
    ("final", "Final / agreed"),
]


class StatementVersion(db.Model):
    """One snapshot of the whole statement set at a point in the review cycle.

    Every round is kept: what we sent, what the customer sent back, and what
    was finally agreed. A version stores a full JSON snapshot of the figures
    rather than pointing at live rows, so an earlier version still shows what
    it showed at the time even after the statements are rebuilt.
    """
    __tablename__ = "statement_versions"
    __table_args__ = (db.UniqueConstraint("financial_year_id", "version_no",
                                          name="uq_version_fy_no"),)

    id = db.Column(db.Integer, primary_key=True)
    financial_year_id = db.Column(db.Integer, db.ForeignKey("financial_years.id"),
                                  nullable=False)
    version_no = db.Column(db.Integer, nullable=False)

    source = db.Column(db.String(20), default="auditor")   # auditor | customer
    status = db.Column(db.String(30), default="draft", nullable=False)

    # Token embedded in the email subject, e.g. AM-2025-0007. A customer's
    # Reply keeps it, which is how their message is matched to this engagement.
    token = db.Column(db.String(40), index=True)

    # Full figures at this point in time.
    snapshot = db.Column(JSON)

    # The workbook we emailed out, and whatever the customer sent back.
    xlsx_path = db.Column(db.String(500))
    pdf_path = db.Column(db.String(500))
    revised_file_path = db.Column(db.String(500))

    customer_comments = db.Column(db.Text)
    notes = db.Column(db.Text)

    sent_at = db.Column(db.DateTime)
    sent_to = db.Column(db.String(255))
    received_at = db.Column(db.DateTime)
    received_from = db.Column(db.String(255))

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    financial_year = db.relationship("FinancialYear", back_populates="versions")
    author = db.relationship("User", foreign_keys=[created_by])

    @property
    def status_label(self):
        return label_for(VERSION_STATUSES, self.status)

    @property
    def is_final(self):
        return self.status == "final"

    @property
    def statement_count(self):
        return len((self.snapshot or {}).get("statements", []))

    def totals(self):
        """Headline figures, for the version list."""
        out = {}
        for statement in (self.snapshot or {}).get("statements", []):
            for line in statement.get("lines", []):
                if line.get("line_key") in ("total_assets", "profit_for_year"):
                    out[line["line_key"]] = line.get("amount")
        return out

    def __repr__(self):
        return f"<StatementVersion v{self.version_no} {self.status}>"


# --------------------------------------------------------------------------
# Audit report
# --------------------------------------------------------------------------

class AuditReport(db.Model):
    __tablename__ = "audit_reports"

    id = db.Column(db.Integer, primary_key=True)
    financial_year_id = db.Column(db.Integer, db.ForeignKey("financial_years.id"),
                                  nullable=False)
    title = db.Column(db.String(255), default="Independent Auditor's Report")
    status = db.Column(db.String(20), default="draft")     # draft | final
    version = db.Column(db.Integer, default=1)
    generated_at = db.Column(db.DateTime, default=datetime.utcnow)
    generated_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    pdf_path = db.Column(db.String(500))

    financial_year = db.relationship("FinancialYear", back_populates="reports")
    sections = db.relationship(
        "AuditReportSection", back_populates="report",
        cascade="all, delete-orphan", order_by="AuditReportSection.sort_order",
    )

    @property
    def enabled_sections(self):
        return [s for s in self.sections if s.is_enabled]


class AuditReportSection(db.Model):
    __tablename__ = "audit_report_sections"

    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey("audit_reports.id"), nullable=False)
    section_key = db.Column(db.String(80), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    section_type = db.Column(db.String(20), default="free_text")  # template|free_text|statement
    sort_order = db.Column(db.Integer, default=0)
    is_enabled = db.Column(db.Boolean, default=True)
    content_html = db.Column(db.Text)
    data_binding = db.Column(JSON)

    # Set when this note's text was carried forward from last year's signed
    # accounts rather than the FRS library. Recorded rather than merged in
    # silently: carried wording is last year's claim about this company, and
    # the preparer has to be told which sentences they are inheriting so
    # they can confirm they are still true.
    prior_note_id = db.Column(db.Integer, db.ForeignKey("prior_year_notes.id"))

    # A sub-note the auditor attached to an existing note - "11.1" rather
    # than its own top-level number. NULL for every ordinary note. Ordering
    # among several children of the same parent is still their own
    # sort_order; they render as a block directly after the parent
    # regardless of where the parent sits among its own siblings.
    parent_section_id = db.Column(db.Integer,
                                  db.ForeignKey("audit_report_sections.id"))

    report = db.relationship("AuditReport", back_populates="sections")
    children = db.relationship(
        "AuditReportSection", backref=db.backref("parent", remote_side=[id]),
        order_by="AuditReportSection.sort_order")


class NoteLibraryEntry(db.Model):
    """The FRS notes catalogue, as a table an auditor can actually add to.

    Seeded once from config/notes_catalogue.yaml (source="spreadsheet") -
    that seed is never edited in place here, so the spreadsheet stays the
    traceable origin of everything that shipped with it. A note an auditor
    adds through the report builder and chooses to save "to the library"
    becomes a second kind of row (source="auditor_added"), from then on
    proposed to every engagement the same way a spreadsheet note is.

    Same shape `services/reports.py` already expects from the YAML - key,
    heading, tick_state, order, trigger_keys, pieces, subsections - kept in
    JSON columns rather than normalised, because a piece's shape already
    varies (a policy paragraph carries wording; a table piece carries
    tb_keys) and the reading code was written against exactly this shape.
    """
    __tablename__ = "note_library_entries"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(120), unique=True, nullable=False)
    heading = db.Column(db.String(255), nullable=False)
    tick_state = db.Column(db.String(20), default="manual", nullable=False)
    sort_order = db.Column(db.Integer, default=500, nullable=False)
    trigger_keys = db.Column(JSON)
    pieces = db.Column(JSON)
    subsections = db.Column(JSON)

    source = db.Column(db.String(20), default="spreadsheet", nullable=False)
    added_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Which engagement and gap prompted an auditor-added note - traceability
    # for a note that did not come from the spreadsheet.
    added_reason = db.Column(db.Text)


class ReportFigureOverride(db.Model):
    """An auditor's edit to one row of a note table.

    Statement lines are stored rows, so an override lives on the row itself.
    Note tables are not - `app/services/notes.py` computes them from the
    trial balance every time the report is rendered, so there is no row to
    write to. This table is that missing home: it holds the auditor's
    wording and figure for one row, addressed by where the row sits.

    Identified by position rather than by the row's `ref`, because plenty of
    rows (totals, tax reconciliation lines, currency rows) have no ref at
    all. If the underlying note is rebuilt with different rows the override
    is dropped rather than applied to the wrong line - see `matches`.
    """

    __tablename__ = "report_figure_overrides"
    __table_args__ = (
        db.UniqueConstraint("report_id", "section_key", "table_index",
                            "row_index", name="uq_report_figure_row"),
    )

    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey("audit_reports.id"),
                          nullable=False)
    section_key = db.Column(db.String(80), nullable=False)
    table_index = db.Column(db.Integer, nullable=False, default=0)
    row_index = db.Column(db.Integer, nullable=False, default=0)

    # What the row said when the override was made. If the note is rebuilt
    # and the row at this position is now a different account, the override
    # is ignored rather than silently applied to someone else's figure.
    anchor_label = db.Column(db.String(255))

    label_override = db.Column(db.String(255))
    amount_override = db.Column(Numeric(18, 2))

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    report = db.relationship("AuditReport")

    def matches(self, row):
        """True when this override still belongs to the row given."""
        if not self.anchor_label:
            return True
        return (row.get("label") or "") == self.anchor_label

    @property
    def is_empty(self):
        return self.label_override is None and self.amount_override is None

    def __repr__(self):
        return (f"<FigureOverride {self.section_key}"
                f"[{self.table_index}][{self.row_index}]>")


# --------------------------------------------------------------------------
# Customer review of the trial balance
#
# The customer gets an emailed link and edits their trial balance in the
# browser. No login, no account, no portal - the random token in the URL is
# the only secret, and they never type it.
# --------------------------------------------------------------------------

class CustomerReviewLink(db.Model):
    """A single-engagement, no-login access token for the customer.

    The raw token exists only in the email that was sent. What is stored here
    is a SHA-256 hash, so a leaked database does not hand anyone working
    links. The token is a bearer credential - whoever holds the URL can see
    that engagement's trial balance - which is why expiry, revocation and
    access logging all matter, and why an optional passcode exists.
    """
    __tablename__ = "customer_review_links"

    id = db.Column(db.Integer, primary_key=True)
    financial_year_id = db.Column(db.Integer, db.ForeignKey("financial_years.id"),
                                  nullable=False, index=True)

    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    # Optional second factor, passed to the client by another channel.
    passcode_hash = db.Column(db.String(255))

    expires_at = db.Column(db.DateTime)
    revoked_at = db.Column(db.DateTime)
    submitted_at = db.Column(db.DateTime)

    access_count = db.Column(db.Integer, default=0, nullable=False)
    last_accessed_at = db.Column(db.DateTime)
    last_accessed_ip = db.Column(db.String(45))

    sent_to = db.Column(db.String(255))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    financial_year = db.relationship("FinancialYear",
                                     back_populates="review_links")

    @property
    def is_expired(self):
        return bool(self.expires_at and datetime.utcnow() > self.expires_at)

    @property
    def is_revoked(self):
        return self.revoked_at is not None

    @property
    def is_usable(self):
        return not self.is_expired and not self.is_revoked

    @property
    def needs_passcode(self):
        return bool(self.passcode_hash)

    @property
    def state_label(self):
        if self.is_revoked:
            return "Revoked"
        if self.is_expired:
            return "Expired"
        if self.submitted_at:
            return "Submitted"
        if self.access_count:
            return "Opened"
        return "Sent, not yet opened"


VERSION_SOURCES = [("auditor", "Prepared by us"), ("customer", "From customer")]

TB_VERSION_STATUSES = [
    ("sent", "Sent to customer"),
    ("customer_submitted", "Customer returned changes"),
    ("applied", "Reviewed and applied"),
]


class TrialBalanceVersion(db.Model):
    """One round of the trial balance review.

    Holds a full JSON snapshot of the figures at that moment, so an earlier
    version still shows what it showed at the time even after the trial
    balance is rebuilt.
    """
    __tablename__ = "trial_balance_versions"
    __table_args__ = (db.UniqueConstraint("financial_year_id", "version_no",
                                          name="uq_tbversion_fy_no"),)

    id = db.Column(db.Integer, primary_key=True)
    financial_year_id = db.Column(db.Integer, db.ForeignKey("financial_years.id"),
                                  nullable=False, index=True)
    version_no = db.Column(db.Integer, nullable=False)

    source = db.Column(db.String(20), default="auditor", nullable=False)
    status = db.Column(db.String(30), default="sent", nullable=False)

    snapshot = db.Column(JSON)
    link_id = db.Column(db.Integer, db.ForeignKey("customer_review_links.id"))

    sent_at = db.Column(db.DateTime)
    sent_to = db.Column(db.String(255))
    submitted_at = db.Column(db.DateTime)
    submitted_from_ip = db.Column(db.String(45))

    customer_message = db.Column(db.Text)
    notes = db.Column(db.Text)

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    financial_year = db.relationship("FinancialYear",
                                     back_populates="tb_versions")
    link = db.relationship("CustomerReviewLink")
    changes = db.relationship(
        "TrialBalanceChange", back_populates="version",
        cascade="all, delete-orphan", order_by="TrialBalanceChange.id")

    @property
    def status_label(self):
        return label_for(TB_VERSION_STATUSES, self.status)

    @property
    def pending_changes(self):
        return [c for c in self.changes if c.status == "pending"]

    @property
    def change_summary(self):
        counts = {"pending": 0, "accepted": 0, "rejected": 0}
        for change in self.changes:
            counts[change.status] = counts.get(change.status, 0) + 1
        return counts


class TrialBalanceChange(db.Model):
    """One figure the customer proposed changing.

    Each is ruled on individually by the auditor - accept, reject, or accept
    with a different value - which is what "select the portion which is
    correct" means in practice.
    """
    __tablename__ = "trial_balance_changes"

    id = db.Column(db.Integer, primary_key=True)
    version_id = db.Column(db.Integer, db.ForeignKey("trial_balance_versions.id"),
                           nullable=False, index=True)
    tb_account_id = db.Column(db.Integer,
                              db.ForeignKey("trial_balance_accounts.id"))

    # Denormalised so the change still reads correctly if the account is
    # later removed from the trial balance.
    account_code = db.Column(db.String(50))
    account_name = db.Column(db.String(255))

    field = db.Column(db.String(10), nullable=False)        # debit | credit
    value_before = db.Column(Numeric(18, 2))
    value_after = db.Column(Numeric(18, 2))
    customer_comment = db.Column(db.Text)

    status = db.Column(db.String(20), default="pending", nullable=False)
    applied_value = db.Column(Numeric(18, 2))   # set if accepted-with-edit
    decided_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    decided_at = db.Column(db.DateTime)
    decision_note = db.Column(db.Text)

    version = db.relationship("TrialBalanceVersion", back_populates="changes")
    account = db.relationship("TrialBalanceAccount")

    @property
    def delta(self):
        return (self.value_after or 0) - (self.value_before or 0)

    @property
    def effective_value(self):
        """What actually gets written if this change is accepted."""
        return (self.applied_value if self.applied_value is not None
                else self.value_after)


# --------------------------------------------------------------------------
# Background jobs + audit trail
# --------------------------------------------------------------------------

class Job(db.Model):
    """Simple DB-backed queue. The worker claims rows with SKIP LOCKED."""
    __tablename__ = "jobs"

    id = db.Column(db.Integer, primary_key=True)
    job_type = db.Column(db.String(40), nullable=False)      # extract_document
    payload = db.Column(JSON)
    status = db.Column(db.String(20), default="queued", nullable=False)
    attempts = db.Column(db.Integer, default=0)
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)


class AuditLog(db.Model):
    """Every material change is recorded here.

    In an audit tool the correction history *is* evidence, so this is written
    on document verification, statement overrides and approval actions.
    """
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    entity_type = db.Column(db.String(50), nullable=False)
    entity_id = db.Column(db.Integer)
    action = db.Column(db.String(80), nullable=False)
    before = db.Column(JSON)
    after = db.Column(JSON)
    ip = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User")


class PriorYearNote(db.Model):
    """A note read out of last year's signed accounts.

    Last year's figures were already treated as required data rather than
    evidence (see services/prior_year.py). Its *words* are data too, and were
    being thrown away: which notes the company actually disclosed, and the
    sentences specific to it - principal activities, credit terms, useful
    lives. Those are not boilerplate a library can supply, because they
    describe this company and no other.

    Stored per financial year rather than per customer: a note's wording can
    change between years, and the auditor needs to see what was said in the
    year being compared against, not the most recent version of it.

    Nothing here is used without a human. This is what last year said, offered
    to the preparer as a starting point - never written into a note unseen.
    """

    __tablename__ = "prior_year_notes"

    id = db.Column(db.Integer, primary_key=True)
    financial_year_id = db.Column(db.Integer,
                                  db.ForeignKey("financial_years.id"),
                                  nullable=False, index=True)
    source_document_id = db.Column(db.Integer, db.ForeignKey("documents.id"))

    # As printed last year: "3", "3(a)", or blank on an unnumbered section
    # like the corporate information that precedes the numbered notes.
    note_number = db.Column(db.String(20))
    title = db.Column(db.String(255), nullable=False)
    body_text = db.Column(db.Text)

    # The library note this appears to correspond to, when one matches.
    # Nullable on purpose: a company-specific note that our library has never
    # heard of is exactly the kind we must not silently drop.
    matched_key = db.Column(db.String(80), index=True)

    confidence = db.Column(db.Float, default=1.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    financial_year = db.relationship("FinancialYear",
                                     back_populates="prior_notes")
    source_document = db.relationship("Document")

    def __repr__(self):
        return f"<PriorYearNote {self.note_number} {self.title!r}>"


class CustomerDocument(db.Model):
    """A document about the company itself, not about one of its years.

    An ACRA Business Profile describes the company - its UEN, its officers,
    what it does - and none of that belongs to a particular financial year.
    The main documents table requires a financial year, and at the moment a
    customer is created there is not one yet, so these live here instead of
    loosening that rule on a table where "which year is this about" is a
    question every other row must answer.

    Kept rather than read and thrown away: it is the evidence behind the
    corporate information note, and an auditor asked where a company's
    principal activities came from should be able to open the profile they
    came from.
    """

    __tablename__ = "customer_documents"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"),
                            nullable=False, index=True)

    kind = db.Column(db.String(40), default="acra_profile", nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)
    storage_path = db.Column(db.String(500), nullable=False)
    file_type = db.Column(db.String(20))
    size_bytes = db.Column(db.BigInteger)
    sha256 = db.Column(db.String(64))

    # What was read out of it, as read, before anyone edited the form. Kept
    # so a later question - "did we type that or did the profile say it?" -
    # has an answer that does not depend on memory.
    extracted = db.Column(JSON)
    extraction_error = db.Column(db.String(255))

    uploaded_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    customer = db.relationship("Customer", back_populates="company_documents")

    def __repr__(self):
        return f"<CustomerDocument {self.kind} {self.original_filename!r}>"
