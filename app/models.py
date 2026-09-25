"""Database models for Auditmate Lite.

The whole schema lives in one module because the aggregates are tightly
related and it keeps imports simple. Roughly it follows the pipeline:

    Customer -> FinancialYear -> Document -> ExtractedLineItem
                              -> FinancialStatement -> StatementLine
                              -> AuditReport -> AuditReportSection

Every money-bearing row keeps a pointer back to where the number came from,
because this is audit software and provenance is the point.
"""
from datetime import datetime, date, timedelta

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
    # A sheet of an accounting system's own "financial reporting" pack: the
    # statements, notes and cover pages it has already drawn FROM the books.
    # Output, not a source - it must never build or overrule the accounts.
    ("reporting_pack", "Reporting pack sheet (output, not a source)"),
    # The compilation report sheet of such a pack, or a firm's own. Read only
    # for the practitioner's name and address, when they are filled in.
    ("compilation_report", "Compilation report"),
    ("other", "Other"),
    # Finished statements the client (or an accounting system) hands over.
    # Reference only, like the reporting pack: never a source, never in
    # TB_SOURCE_PRECEDENCE, so they cannot build or overrule the accounts.
    ("fs_comprehensive_income", "Statement of Comprehensive Income"),
    ("fs_financial_position", "Statement of Financial Position"),
    ("fs_indirect_cash_flow", "Statement of Indirect Cash Flow"),
    ("fs_direct_cash_flow", "Statement of Direct Cash Flow"),
    ("fs_changes_in_equity", "Statement of Changes in Equity"),
    ("fs_detailed_pl", "Statement of Detailed Profit and Loss"),
    # The prior-year half of every OTHER category a client might also send
    # twice - the same reasoning PRIOR_YEAR_TWIN gives for a trial balance
    # applies just as much to a bank statement or an aged listing: a client
    # sends last year's alongside this year's, nothing in the file says
    # which is which, and without a place to say so the wrong year's evidence
    # silently counts as this year's. Not offered directly in the plain
    # category dropdown (see CURRENT_YEAR_TWIN filtering in
    # documents/index.html) - reached through the Year column instead, same
    # as the trial balance and cash flow twins above.
    ("prior_balance_sheet", "Balance Sheet (prior year)"),
    ("prior_profit_and_loss", "Profit & Loss / Income Statement (prior year)"),
    ("prior_general_ledger", "General Ledger (prior year)"),
    ("prior_bank_statement", "Bank Statement (prior year)"),
    ("prior_vendor_invoice", "Vendor Invoice (prior year)"),
    ("prior_customer_invoice", "Customer Invoice (prior year)"),
    ("prior_salary_schedule", "Salary Schedule (prior year)"),
    ("prior_payables", "Accounts Payable Listing (prior year)"),
    ("prior_receivables", "Accounts Receivable Listing (prior year)"),
    ("prior_fixed_asset_register", "Fixed Asset Register (prior year)"),
    ("prior_tax_document", "Tax Document / GST Return (prior year)"),
    ("prior_other", "Other (prior year)"),
]

# How the category dropdowns are grouped: the client's management accounts (the
# books and the papers that support them) apart from finished financial
# statements. Current-year categories only - last year's are reached through the
# Year column.
CATEGORY_GROUPS = [
    ("Management accounts", [
        "trial_balance", "balance_sheet", "profit_and_loss", "cash_flow",
        "general_ledger", "bank_statement", "vendor_invoice", "customer_invoice",
        "salary_schedule", "payables", "receivables", "fixed_asset_register",
        "tax_document"]),
    ("Financial statements", [
        "fs_comprehensive_income", "fs_financial_position", "fs_indirect_cash_flow",
        "fs_direct_cash_flow", "fs_changes_in_equity", "fs_detailed_pl",
        "signed_accounts", "reporting_pack"]),
    ("Other", ["compilation_report", "other"]),
]

# Categories that come in a current-year / prior-year pair.
#
# A client sends last year's trial balance and this year's, and nothing in
# either file reliably says which is which - the auto-detector reads what a
# document IS, not which year it describes. So the year is the auditor's to
# state, and it is stored as the category rather than as a separate field:
# every downstream decision (what builds the accounts, what only supplies the
# comparative column) already keys off the category.
PRIOR_YEAR_TWIN = {
    "trial_balance": "prior_trial_balance",
    "cash_flow": "prior_cash_flow",
    "balance_sheet": "prior_balance_sheet",
    "profit_and_loss": "prior_profit_and_loss",
    "general_ledger": "prior_general_ledger",
    "bank_statement": "prior_bank_statement",
    "vendor_invoice": "prior_vendor_invoice",
    "customer_invoice": "prior_customer_invoice",
    "salary_schedule": "prior_salary_schedule",
    "payables": "prior_payables",
    "receivables": "prior_receivables",
    "fixed_asset_register": "prior_fixed_asset_register",
    "tax_document": "prior_tax_document",
    "other": "prior_other",
}
CURRENT_YEAR_TWIN = {prior: current for current, prior in PRIOR_YEAR_TWIN.items()}


def category_for_year(category, is_prior):
    """The stored category for a base category plus the year chosen."""
    base = CURRENT_YEAR_TWIN.get(category, category)
    if not is_prior:
        return base
    return PRIOR_YEAR_TWIN.get(base, base)


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

# Two logins, identical day-to-day access. The one difference is what
# happens to a customer once it's archived: staff can archive and restore
# one, same as a partner - only permanently deleting one, and managing
# other logins, is reserved to a partner. See services.permissions.
ROLE_PARTNER = "partner"
ROLE_STAFF = "staff"
ROLES = [
    (ROLE_PARTNER, "Partner"),
    (ROLE_STAFF, "Staff"),
]


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default=ROLE_PARTNER, nullable=False)
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
    def is_partner(self) -> bool:
        """Whether this login can permanently delete a customer or manage
        other logins.

        Deliberately the *inverse* check - anything not explicitly "staff"
        counts as a partner, rather than requiring an exact "partner"
        match. A login created before these two roles existed carries
        whatever value it always had (this app's earlier "admin"/"auditor"
        text), and treating an unrecognised value as staff would silently
        lock a working partner out of screens they used to have. Staff is
        the narrower, deliberately-granted role; partner is everyone else.
        """
        return self.role != ROLE_STAFF

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

    # WHETHER THIS CLIENT'S DATA MAY LEAVE THE MACHINE.
    #
    # Off unless somebody turns it on, for this client, deliberately. A
    # model that helps map a chart of accounts is a model somebody else
    # runs, and an account name is the client's business, not ours: "Loan
    # - K Tan" names a director, "Retention - Sembcorp" names a customer
    # and the contract they are on.
    #
    # Default-off rather than default-on with an opt-out, because the cost
    # of the two mistakes is not symmetric. Forgetting to switch it on
    # means somebody maps a few accounts by hand. Forgetting to switch it
    # off means a real client's books went to a third party, and there is
    # no taking that back.
    ai_allowed = db.Column(db.Boolean, default=False, nullable=False)
    ai_allowed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    ai_allowed_at = db.Column(db.DateTime)
    ai_allowed_note = db.Column(db.String(255))

    # Custom report template uploaded by the customer. When set, final reports
    # for this customer use their template's structure/styling instead of the
    # built-in standard template. Stored at the Customer level so all their
    # financial years inherit the same template, per the firm's workflow.
    report_template_path = db.Column(db.String(500))
    report_template_uploaded_at = db.Column(db.DateTime)

    # Archived, not deleted: hidden from the customer list, every document
    # and figure kept. Reusing this existing flag rather than adding a new
    # one - it was already here, already meant "on the list or not", and
    # was simply never wired to anything until now (see dashboard.py's
    # count, the only place that ever read it before this).
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    archived_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    # Bumped on every view or edit (see customers.py detail()/edit()) so the
    # customer list can surface whoever was worked on most recently. Kept
    # separate from updated_at, which is a real audit-trail field and should
    # only change when data actually changes, not just from being viewed.
    last_activity_at = db.Column(db.DateTime)

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
    # Figures taken from documents the engine does not read (the tax
    # computation, last year's signed notes). Without a cascade the year
    # could not be deleted once any had been stored.
    document_figures = db.relationship(
        "DocumentFigure", back_populates="financial_year",
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

    # Which notes library this engagement reports under. Chosen once, from
    # the year end, and not changed afterwards: a later library must not
    # rewrite the wording of a period already reported on. NULL means the
    # engagement predates versioning and falls back to the flat catalogue.
    library_version_id = db.Column(db.Integer,
                                   db.ForeignKey("note_library_versions.id"),
                                   index=True)

    prior_notes = db.relationship(
        "PriorYearNote", back_populates="financial_year",
        cascade="all, delete-orphan")
    fixed_asset_register_items = db.relationship(
        "FixedAssetRegisterItem", back_populates="financial_year",
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
    def previous_period_end(self):
        """The date last year's period ended, for the comparative column.

        Taken from the linked previous engagement when there is one, and
        otherwise from this period's own start - the day before it began.
        The comparative column needs a heading whenever it carries figures,
        and those figures often come from last year's signed accounts or the
        prior column of this year's trial balance rather than from a previous
        engagement in Auditmate. Without the fallback the column of figures
        was headed with a dash.

        None for a first financial period, which has no prior year at all.
        """
        if self.is_first_year:
            return None
        if self.previous_year and self.previous_year.end_date:
            return self.previous_year.end_date
        if self.start_date:
            return self.start_date - timedelta(days=1)
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

    # An auditor's standing correction that this document's two years print
    # backwards - see documents.swap_years. Re-extraction rebuilds every
    # ExtractedLineItem from scratch (a fresh AI/rule-based read, a different
    # sheet chosen), which used to silently discard a swap already applied:
    # the correction lived only on the rows just deleted. Recorded here so
    # extract_document can re-apply it every time, the same way a manual
    # category never reverts to a guessed one.
    periods_swapped = db.Column(db.Boolean, default=False, nullable=False)

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

    # Why a person typed over the computed figure, and what it was at the
    # time. Library 3.5 asks this of every overridden figure (OV-03, OV-04);
    # a line on the face of the statements is no different from a row in a
    # note, and the reviewer reads the two side by side.
    override_reason = db.Column(db.Text)
    override_source_amount = db.Column(Numeric(18, 2))
    override_at = db.Column(db.DateTime)
    override_by = db.Column(db.Integer, db.ForeignKey("users.id"))

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
    def override_record(self):
        """What the reviewer needs about a figure typed over on the face.

        The same shape a note row carries, so one template macro marks
        both (library 3.5, OV-05): a reviewer should not have to read the
        balance sheet and the notes in two different ways.
        """
        if not self.is_overridden or not self.override_reason:
            return None
        person = db.session.get(User, self.override_by) \
            if self.override_by else None
        return {
            "source_amount": self.override_source_amount,
            "source_label": self.label,
            "source_name": ("the lines it adds up" if self.formula
                            else "the trial balance"),
            "reason": self.override_reason,
            "who": (getattr(person, "name", None)
                    or getattr(person, "email", None) or "a preparer"),
            "when": self.override_at,
        }

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

    # The notes library's finer category for this account - the line code
    # every note table row takes its figure from. standard_key builds the
    # face of the statements; line_code builds the notes. See
    # config/line_code_categories.yaml and services/line_codes.py.
    line_code = db.Column(db.String(20), index=True)
    # Where it came from, because a settled category and a proposed one must
    # not look the same on the mapping screen:
    #   only     the statement line allows one code - nothing to decide
    #   rule     proposed from the account's name or side
    #   default  proposed as the line's usual code, nothing more specific
    #   carried  the same account's category last year
    #   manual   a person chose it
    line_code_source = db.Column(db.String(12))

    debit = db.Column(Numeric(18, 2), default=0)
    credit = db.Column(Numeric(18, 2), default=0)

    # Last year's figure for the SAME account, carried off the prior-year
    # column of the document that built this one. It is stored here rather
    # than read from the document at statement time so that one account holds
    # both years and ONE mapping drives both columns - re-map an account and
    # last year moves with it. Null where the source carried no comparative,
    # which is not the same as nil and must never read as nil.
    prior_debit = db.Column(Numeric(18, 2))
    prior_credit = db.Column(Numeric(18, 2))

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

    # Where the statement line came from when a rule did not guess it:
    #   "previous_year" - the same account, mapped in this client's previous
    #                     engagement, taken over as it stood
    #   "ai"            - an accepted AI suggestion
    #   "manual"        - a person chose it here
    # Empty for a rule's own guess. mapping_is_manual stays the flag a rebuild
    # keeps; this only says why the line is what it is.
    mapping_source = db.Column(db.String(20))

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
    # Sections the last render found incomplete (a question unanswered, a
    # figure unsourced, a blank unfilled). None until first rendered. Kept so
    # a dashboard can say "Incomplete" without rendering every report.
    incomplete_notes = db.Column(db.Integer)
    completeness_checked_at = db.Column(db.DateTime)
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


class NoteLibraryVersion(db.Model):
    """One import of the FRS notes library, valid for a range of year ends.

    The library is versioned by financial year end because wording in force
    for one period is not in force for another: version 1.0 covers year ends
    to 31 December 2026, and FRS 118 requires a second version for periods
    beginning 1 January 2027. Both have to exist at once - an FY2026 and an
    FY2027 engagement can be open in the same week and must not share a
    rulebook.

    An engagement pins itself to a version and never moves, for the same
    reason an approved trial balance does not move: a later change must not
    reach backwards into a period already reported on.
    """
    __tablename__ = "note_library_versions"

    id = db.Column(db.Integer, primary_key=True)
    version_label = db.Column(db.String(20), unique=True, nullable=False)
    framework = db.Column(db.String(160))
    entity_scope = db.Column(db.Text)

    # What an engagement matches its own year end against.
    valid_from = db.Column(db.Date, nullable=False)
    valid_to = db.Column(db.Date, nullable=False)

    # Which workbook produced this. The digest is what stops the same file
    # being imported twice by accident, and lets a loaded version be traced
    # back to the file it came from.
    source_filename = db.Column(db.String(255))
    source_sha256 = db.Column(db.String(64), index=True)

    imported_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    imported_by = db.Column(db.Integer, db.ForeignKey("users.id"))

    # draft      - imported, not yet offered to engagements
    # active     - new engagements may pin to it
    # superseded - kept because engagements are still pinned to it
    status = db.Column(db.String(20), default="draft", nullable=False)
    notes_count = db.Column(db.Integer, default=0, nullable=False)

    notes = db.relationship("NoteLibraryNote", back_populates="library_version",
                            cascade="all, delete-orphan",
                            order_by="NoteLibraryNote.sort_order")
    sheets = db.relationship("NoteLibrarySheet", back_populates="library_version",
                             cascade="all, delete-orphan",
                             order_by="NoteLibrarySheet.name")

    def sheet(self, name):
        """Rows of one reference sheet in this version, or an empty list."""
        for held in self.sheets:
            if held.name == name:
                return held.rows or []
        return []

    def covers(self, year_end):
        """Whether a period ending on this date belongs to this version."""
        if year_end is None:
            return False
        return self.valid_from <= year_end <= self.valid_to

    @property
    def period_label(self):
        return (f"{self.valid_from:%d %b %Y} to {self.valid_to:%d %b %Y}"
                if self.valid_from and self.valid_to else "-")

    def __repr__(self):
        return f"<NoteLibraryVersion {self.version_label}>"


class NoteLibrarySheet(db.Model):
    """One reference sheet of a library version, held as the rows it carries.

    Version 1 of the library was five sheets and the notes table held all of
    it. Version 2 added fifteen more that the notes engine reads rather than
    prints - the statement lines every figure binds to, the source document
    behind each binding token, the firm settings and their defaults, the
    preparer questions, the alias table for last year's headings, and so on.

    Kept per version, not as shared lookup tables, for the reason the notes
    are: the line codes, the questions and the defaults are part of the
    library in force for a period. FRS 118 will change several of them for
    2027, and an FY2026 engagement must keep reading the 2026 set.

    Held as JSON rather than one table per sheet. The engine reads a sheet
    whole and small - the largest is 326 rows - and a table per sheet would
    tie the schema to one version's column layout, which the client has
    already changed four times.
    """
    __tablename__ = "note_library_sheets"
    __table_args__ = (db.UniqueConstraint("library_version_id", "name",
                                          name="uq_note_library_sheets_version_name"),)

    id = db.Column(db.Integer, primary_key=True)
    library_version_id = db.Column(db.Integer,
                                   db.ForeignKey("note_library_versions.id"),
                                   nullable=False, index=True)
    # The sheet's own tab name, exactly as the workbook spells it.
    name = db.Column(db.String(80), nullable=False)
    rows = db.Column(JSON)
    row_count = db.Column(db.Integer, default=0, nullable=False)

    library_version = db.relationship("NoteLibraryVersion",
                                      back_populates="sheets")


class NoteLibraryNote(db.Model):
    """One note or sub-section belonging to one library version.

    Deliberately a separate table from `note_library_entries` rather than a
    version column on it. That table's `key` is unique, which is correct for
    a firm's own additions but collides the moment the same note arrives
    again in a second library version. Keeping them apart also means an
    import can never touch a note an auditor wrote.
    """
    __tablename__ = "note_library_notes"
    __table_args__ = (
        db.UniqueConstraint("library_version_id", "key",
                            name="uq_note_library_notes_version_key"),
    )

    id = db.Column(db.Integer, primary_key=True)
    library_version_id = db.Column(db.Integer,
                                   db.ForeignKey("note_library_versions.id"),
                                   nullable=False, index=True)

    # `key` is what the report engine already keys against, so a note we
    # already held keeps the key it has always had - existing report
    # sections reference it, including in reports already issued and frozen.
    key = db.Column(db.String(120), nullable=False, index=True)
    # The library's own code. Column B on the Notes sheet - the one every
    # other sheet in the workbook joins on.
    library_code = db.Column(db.String(120), index=True)
    # Column K, the NOTE_/POL_ alias. Carried for traceability only.
    library_code_alt = db.Column(db.String(120))

    heading = db.Column(db.String(255), nullable=False)
    tick_state = db.Column(db.String(20), default="manual", nullable=False)
    sort_order = db.Column(db.Integer, default=500, nullable=False)

    trigger_keys = db.Column(JSON)
    pieces = db.Column(JSON)
    subsections = db.Column(JSON)

    # Structure and provenance the workbook carries and the old catalogue
    # had nowhere to put.
    section_no = db.Column(db.Integer)
    section_name = db.Column(db.String(160))
    standards = db.Column(db.String(255))
    presented_as = db.Column(db.String(40))   # Numbered note | Sub-section
    sits_inside = db.Column(db.String(255))
    # The trigger as the library words it, in English. Kept verbatim so the
    # conversion to real conditions stays reviewable - see Stage 3.
    trigger_text = db.Column(db.Text)

    library_version = db.relationship("NoteLibraryVersion", back_populates="notes")

    def __repr__(self):
        return f"<NoteLibraryNote {self.key}>"


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


# How a related party is related. The list a Singapore SME actually
# needs, in the order a preparer thinks of them.
RELATED_PARTY_KINDS = [
    ("director", "Director"),
    ("shareholder", "Shareholder"),
    ("key_management", "Key management personnel"),
    ("close_family", "Close family member of a director or shareholder"),
    ("related_company", "Related company"),
    ("other", "Other related party"),
]


class RelatedParty(db.Model):
    """A person or company this engagement transacts with as a related party.

    Entered once per engagement and carried forward, because a director
    does not change between years.

    It exists because nothing in the books can produce it. The notes
    library's own Known issues sheet, KI-01: every entry in the test
    client's director loan account is Spend Money or Receive Money, so no
    parsing rule recovers the counterparty, and the director appears under
    four spellings. Relatedness is a fact about people, and the only
    reliable source for it is the preparer.

    `spellings` holds the names the party appears under in the books -
    "Loan from Director", "A Tan", "Mr Tan Ah Kow", "TAK". They are what
    the matcher suggests on, never what it decides on: a suggestion is
    shown back and a person confirms it. A name that merely looks similar
    is the commonest way an unrelated supplier ends up disclosed as a
    director's company.
    """

    __tablename__ = "related_parties"

    id = db.Column(db.Integer, primary_key=True)
    financial_year_id = db.Column(db.Integer,
                                  db.ForeignKey("financial_years.id"),
                                  nullable=False, index=True)

    # As it should read in the accounts.
    name = db.Column(db.String(255), nullable=False)
    kind = db.Column(db.String(30), default="director", nullable=False)

    # Every spelling this party appears under in the books.
    spellings = db.Column(JSON)

    # Free text the preparer wants the reviewer to see - "sole director,
    # also owns the landlord company".
    note = db.Column(db.Text)

    # Carried from last year, so a preparer can see what they inherited
    # rather than what they decided this year.
    carried_from_id = db.Column(db.Integer,
                                db.ForeignKey("related_parties.id"))

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    financial_year = db.relationship("FinancialYear")
    carried_from = db.relationship("RelatedParty", remote_side=[id])
    matches = db.relationship("RelatedPartyMatch", back_populates="party",
                              cascade="all, delete-orphan")

    @property
    def kind_label(self):
        return dict(RELATED_PARTY_KINDS).get(self.kind, self.kind)

    @property
    def all_names(self):
        """Every string this party is known by, the printed name included."""
        names = [self.name] + list(self.spellings or [])
        seen, out = set(), []
        for name in names:
            key = " ".join(str(name or "").split()).lower()
            if key and key not in seen:
                seen.add(key)
                out.append(name)
        return out

    def __repr__(self):
        return f"<RelatedParty {self.name}>"


class RelatedPartyMatch(db.Model):
    """One thing in the books, decided to be with a related party - or not.

    Addressed by subject type and id rather than by a foreign key, so the
    same record serves a trial balance account today and a general ledger
    entry when a ledger is loaded. The notes need both: the balances come
    from mapped accounts, the transactions from entries.

    A row exists only once a person has decided. A suggestion the engine
    made and nobody has looked at is not stored, because a stored
    suggestion read later is indistinguishable from a decision - and the
    whole point of KI-01 is that the matches are shown back and confirmed.

    A REJECTION IS A DECISION TOO, and is kept. Without it the same wrong
    suggestion comes back every time the page is opened, and the preparer
    who dismissed it last month has no way to show that they did.
    """

    __tablename__ = "related_party_matches"
    __table_args__ = (
        db.UniqueConstraint("financial_year_id", "subject_type", "subject_id",
                            name="uq_related_party_match_subject"),
    )

    id = db.Column(db.Integer, primary_key=True)
    financial_year_id = db.Column(db.Integer,
                                  db.ForeignKey("financial_years.id"),
                                  nullable=False, index=True)

    # NULL where the decision was "this is not a related party at all".
    party_id = db.Column(db.Integer, db.ForeignKey("related_parties.id"))

    subject_type = db.Column(db.String(20), nullable=False)   # tb_account
    subject_id = db.Column(db.Integer, nullable=False)

    # What the subject was called when the decision was made, so a later
    # rename shows as a changed subject rather than silently carrying the
    # decision onto something else.
    subject_label = db.Column(db.String(255))

    # confirmed | rejected
    decision = db.Column(db.String(20), default="confirmed", nullable=False)

    # Why the engine put this pair in front of a person: the spelling it
    # matched on. Kept so a reviewer can see what the suggestion rested on.
    matched_on = db.Column(db.String(255))

    decided_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    decided_at = db.Column(db.DateTime, default=datetime.utcnow)

    party = db.relationship("RelatedParty", back_populates="matches")

    @property
    def is_related(self):
        return self.decision == "confirmed" and self.party_id is not None

    def __repr__(self):
        return (f"<RelatedPartyMatch {self.subject_type}:{self.subject_id} "
                f"{self.decision}>")


class DocumentFigure(db.Model):
    """One figure taken from a document the engine does not read.

    Most figures in a set of accounts come from the trial balance, and
    AuditMate reads that. A dozen do not: the tax computation, the fixed
    asset register, the aged listing, last year's signed accounts. The
    notes library names each of them as a token and a field - TAX:current,
    FAR:closing_nbv - and until somebody supplies one, the row that binds
    to it prints "Incomplete" rather than nil.

    This is where a supplied one lives. Entered rather than parsed, which
    for the tax computation is the firm's own instruction: five figures
    for a company this size, and writing a parser for a document that
    arrives as a PDF in a different layout every year would cost more than
    it saves.

    What is kept is what makes the figure reviewable a year later: the
    value, where in the document it was found, who entered it and when. It
    is not an override - nothing was assembled for it to sit on top of -
    so it carries no reason. Changing one later is recorded in the audit
    trail like any other edit.
    """

    __tablename__ = "document_figures"
    __table_args__ = (
        db.UniqueConstraint("financial_year_id", "token", "field", "scope",
                            "member", name="uq_document_figure"),
    )

    id = db.Column(db.Integer, primary_key=True)
    financial_year_id = db.Column(db.Integer,
                                  db.ForeignKey("financial_years.id"),
                                  nullable=False, index=True)

    # The library's own binding, split: "TAX" + "current" is TAX:current.
    token = db.Column(db.String(20), nullable=False)
    field = db.Column(db.String(60), nullable=False)

    # Which table the figure belongs to, where the same field name means
    # different figures in different notes. PRIORFS:cost_open_py appears in
    # the plant and equipment note, the investment property note and the
    # intangibles note, and they are three different balances. Empty string
    # rather than NULL: a unique constraint does not constrain NULLs, so a
    # scopeless field could otherwise be entered twice.
    scope = db.Column(db.String(60), default="", nullable=False)

    # Which column within that table. A fixed asset note is presented by
    # class of asset - computers, motor vehicles, renovation - and the
    # register states every movement per class, so "additions during the
    # year" is not one figure but one per class. Empty string for a field
    # that is not presented by class.
    member = db.Column(db.String(80), default="", nullable=False)

    amount = db.Column(Numeric(18, 2))
    text = db.Column(db.Text)                  # for a field that is not a figure

    # Where in the document it was found - "YA2024 computation, line 14".
    # Optional, and worth having: a figure with no page reference is one
    # the next person has to find again from scratch.
    found_at = db.Column(db.String(255))

    entered_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    entered_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    financial_year = db.relationship("FinancialYear",
                                     back_populates="document_figures")
    author = db.relationship("User")

    @property
    def binding(self):
        return f"{self.token}:{self.field}"

    @property
    def where(self):
        """The binding with the note and column it belongs to."""
        at = f"{self.scope}/{self.binding}" if self.scope else self.binding
        return f"{at} [{self.member}]" if self.member else at

    @property
    def is_answered(self):
        """Nil is an answer. Nothing typed at all is not.

        The distinction the whole engine turns on: a preparer who enters
        zero has said the figure is zero, and the row prints a dash. A
        preparer who has not reached this field yet has said nothing, and
        the row prints Incomplete.
        """
        return self.amount is not None or bool((self.text or "").strip())

    def __repr__(self):
        return f"<DocumentFigure {self.binding}>"


# How the library asks each of its 29 preparer inputs to be handled, and
# what happens to the note when nobody answers. Both columns of the
# Preparer inputs sheet, kept here because the same words appear on the
# form, in the service and in the checks.
INPUT_MODES = (
    ("Derive", "Concluded from what is already held"),
    ("Propose", "Worked out and put up for confirmation"),
    ("Ask", "Only a person knows"),
)
INPUT_HOLD = "Hold"       # the note prints Incomplete until it is answered
INPUT_OMIT = "Omit"       # the rows drop out, and the note prints without them


class PreparerInput(db.Model):
    """One of the library's 29 questions, answered for this engagement.

    The Preparer inputs sheet is the library's own account of everything a
    set of accounts needs that no trial balance holds: whether anything is
    pledged, what the directors were paid, whether an invoice was factored.
    Twenty-nine of them, each naming the note it feeds, when it is worth
    asking, and what becomes of the note if nobody answers.

    ANSWERED AND NIL ARE DIFFERENT, the same way they are for a figure
    typed off a document. A preparer who answers "no, nothing is pledged"
    has told the reader something, and the note says so. A preparer who
    has not reached the question yet has told them nothing, and a note
    that would print either way must not pretend otherwise. So `decided`
    is what separates the two, not whether the text is empty: "no" is an
    answer with no text.

    WHY THE MODE IS STORED ON THE ANSWER as well as on the sheet. A later
    version of the library can change its mind about whether something is
    derived or asked - and if it does, an answer given under the old mode
    should still say which question was being answered. Copied at the time
    the answer is given, not looked up afterwards.
    """

    __tablename__ = "preparer_inputs"
    __table_args__ = (
        db.UniqueConstraint("financial_year_id", "item",
                            name="uq_preparer_input"),
    )

    id = db.Column(db.Integer, primary_key=True)
    financial_year_id = db.Column(db.Integer,
                                  db.ForeignKey("financial_years.id"),
                                  nullable=False, index=True)

    # The sheet's own key - PLEDGED, KMP_SPLIT, FACTORING - or, for one of
    # the twelve blanks the Fields sheet ties to a paragraph rather than to
    # a note, "field.CONTINGENT_LIABILITY_NATURE".
    item = db.Column(db.String(80), nullable=False)
    mode = db.Column(db.String(12), default="Ask", nullable=False)

    # The answer itself. `decided` is the tri-state the note reads: unset
    # means nobody has answered, which is not the same as answering no.
    decided = db.Column(db.Boolean, default=False, nullable=False)
    answer = db.Column(db.Text)                 # what the preparer wrote
    amount = db.Column(Numeric(18, 2))          # where the answer is a figure

    # What was put in front of them when they answered. A proposal the
    # preparer accepted unchanged and a figure they typed from scratch are
    # different acts, and a reviewer a year later needs to tell them apart.
    proposed = db.Column(db.Text)
    accepted_proposal = db.Column(db.Boolean, default=False, nullable=False)

    # Where the answer came from, in the preparer's words - "confirmed with
    # the director, 14 March". Optional, and the first thing a reviewer
    # looks for.
    source = db.Column(db.String(255))

    # A question that fills several rows of a note, answered in several
    # parts: [{"label": "Employer CPF", "amount": "44315.00"}, ...].
    #
    # The sheet says how many rows each question fills - five for key
    # management personnel, ten for fair value - but not what those rows
    # are called. So the preparer names each part as well as figuring it,
    # and one box for a five-part question is replaced by five.
    parts = db.Column(db.JSON)

    carried_from_id = db.Column(db.Integer,
                                db.ForeignKey("preparer_inputs.id"))

    decided_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    decided_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    financial_year = db.relationship("FinancialYear")
    author = db.relationship("User", foreign_keys=[decided_by])
    carried_from = db.relationship("PreparerInput", remote_side=[id])

    @property
    def is_answered(self):
        """Answered at all - including answered "no"."""
        return bool(self.decided)

    @property
    def is_paragraph_blank(self):
        """One of the Fields sheet's blanks rather than a note question."""
        return self.item.startswith("field.")

    @property
    def who(self):
        return self.author.name if self.author else "Unknown"

    def __repr__(self):
        return f"<PreparerInput {self.item} decided={self.decided}>"


class AiMappingSuggestion(db.Model):
    """What a model said about one account, and what a person did with it.

    Kept rather than applied. The mapping itself is written only when
    somebody accepts, through the same path a dropdown uses, so an
    accepted suggestion is a person's decision with a record of where the
    idea came from - not a mapping of unknown parentage that a reviewer a
    year later cannot tell from the firm's own work.

    A rejection is kept too. Asking the same model the same question next
    week and re-offering an answer somebody has already turned down is how
    a reviewer learns to click through the list without reading it.

    The model's own words are kept verbatim. "Read from 'Retention' - this
    is money held back on a construction contract" is checkable; a bare
    code is not, and a suggestion nobody can check is one nobody should
    accept.
    """

    __tablename__ = "ai_mapping_suggestions"
    __table_args__ = (
        db.UniqueConstraint("financial_year_id", "account_id",
                            name="uq_ai_mapping_suggestion"),
    )

    id = db.Column(db.Integer, primary_key=True)
    financial_year_id = db.Column(db.Integer,
                                  db.ForeignKey("financial_years.id"),
                                  nullable=False, index=True)
    account_id = db.Column(db.Integer,
                           db.ForeignKey("trial_balance_accounts.id"),
                           nullable=False, index=True)

    # Empty where the model declined, which it is told it may do. An
    # honest "I do not know" for "Sundry 2" is worth more than a confident
    # guess, because the confident guess is the one that gets accepted
    # without being read.
    code = db.Column(db.String(40))
    reason = db.Column(db.Text)

    # Which model, so that a suggestion can be judged by where it came
    # from and so a change of provider is visible in the record.
    model = db.Column(db.String(80))

    decision = db.Column(db.String(12))          # accepted / rejected / None
    requested_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    decided_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    decided_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    financial_year = db.relationship("FinancialYear")
    account = db.relationship("TrialBalanceAccount")
    decider = db.relationship("User", foreign_keys=[decided_by])

    @property
    def is_settled(self):
        return self.decision is not None

    def __repr__(self):
        return f"<AiMappingSuggestion {self.account_id} -> {self.code!r}>"


# table_index for a paragraph override: no table has index -1.
PARAGRAPH_TABLE_INDEX = -1


class ReportFigureOverride(db.Model):
    """One place where a person typed over what the engine assembled.

    A figure on a note table, or a paragraph of a note's wording. Library
    3.5 makes both overridable everywhere (Overrides sheet, OV-01 and
    OV-02): the engine assembles and does not verify, so the preparer must
    be able to correct anything it produced. What the library asks in
    return is that the intervention is visible - OV-03 lists what has to be
    kept, and OV-04 makes the reason compulsory:

        the figure as the source gave it   source_amount / source_text
        the figure as printed              amount_override / text_override
        who changed it                     created_by, updated_by
        when                               created_at, updated_at
        and the reason they gave           reason

    Clearing does not delete the row (OV-07). `cleared_at` is stamped, the
    source figure comes back, and the record of what was changed and why
    stays for the reviewer. Every set and clear also writes a
    ReportOverrideEvent, so a figure typed over three times keeps all three.

    An override changes what the accounts print and nothing else (OV-08):
    the trial balance, the register and the listing are left as they are.

    A FIGURE is addressed by position, because plenty of rows (totals, tax
    reconciliation lines, currency rows) have no ref at all. If the note is
    rebuilt with different rows the override is shown as stale rather than
    applied to the wrong line - see `matches`.

    A PARAGRAPH is addressed the other way round: table_index is -1, the
    row index is a per-section sequence that is never reused, and the
    paragraph in the stored wording carries `data-override="<id>"`, so the
    edit stays with its sentence however the note is reordered.
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

    # A paragraph of wording instead of a row of a table (OV-02).
    para_id = db.Column(db.String(40))
    text_override = db.Column(db.Text)

    # What the source said at the moment the override was made (OV-03).
    # Kept even after the override is cleared, so a reviewer reading the
    # record a year later can still see what was changed.
    source_amount = db.Column(Numeric(18, 2))
    source_label = db.Column(db.String(255))
    source_text = db.Column(db.Text)

    # Where that figure came from, in words - "the trial balance", "the
    # fixed asset register". The reviewer's first question about a typed
    # figure is what it was typed over.
    source_name = db.Column(db.String(120))

    # OV-04. Nullable in the database because every column added to a live
    # table is; the service refuses to write an override without one.
    reason = db.Column(db.Text)

    # OV-07: cleared, not deleted.
    cleared_at = db.Column(db.DateTime)

    # OV-06: the override this one was carried forward from, so a figure
    # inherited as a comparative still carries the history of its change.
    carried_from_id = db.Column(db.Integer,
                                db.ForeignKey("report_figure_overrides.id"))

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    report = db.relationship("AuditReport")
    author = db.relationship("User", foreign_keys=[created_by])
    editor = db.relationship("User", foreign_keys=[updated_by])
    carried_from = db.relationship("ReportFigureOverride", remote_side=[id])
    events = db.relationship(
        "ReportOverrideEvent", back_populates="override",
        cascade="all, delete-orphan",
        order_by="ReportOverrideEvent.at")

    @property
    def is_paragraph(self):
        return self.table_index == PARAGRAPH_TABLE_INDEX

    @property
    def is_live(self):
        """Applied to the report right now, rather than kept as history."""
        return self.cleared_at is None and not self.is_empty

    def matches(self, row):
        """True when this override still belongs to the row given."""
        if not self.anchor_label:
            return True
        return (row.get("label") or "") == self.anchor_label

    @property
    def is_empty(self):
        return (self.label_override is None and self.amount_override is None
                and self.text_override is None)

    @property
    def who(self):
        person = self.editor or self.author
        return (getattr(person, "name", None)
                or getattr(person, "email", None) or "a preparer")

    def __repr__(self):
        return (f"<FigureOverride {self.section_key}"
                f"[{self.table_index}][{self.row_index}]>")


class ReportOverrideEvent(db.Model):
    """Every time an override was set, changed or cleared.

    The override row holds what is true now. This holds what was true
    before, which is what OV-07 asks for: a withdrawn override is still
    something a reviewer may want to see, and so is the second reason
    someone gave when they typed over the same figure again.
    """

    __tablename__ = "report_override_events"

    id = db.Column(db.Integer, primary_key=True)
    override_id = db.Column(db.Integer,
                            db.ForeignKey("report_figure_overrides.id"),
                            nullable=False, index=True)

    # set | changed | cleared | carried
    action = db.Column(db.String(20), nullable=False)
    field = db.Column(db.String(20))               # amount | label | wording
    from_value = db.Column(db.Text)
    to_value = db.Column(db.Text)
    reason = db.Column(db.Text)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    at = db.Column(db.DateTime, default=datetime.utcnow)

    override = db.relationship("ReportFigureOverride", back_populates="events")
    user = db.relationship("User")

    @property
    def who(self):
        return (getattr(self.user, "name", None)
                or getattr(self.user, "email", None) or "a preparer")

    def __repr__(self):
        return f"<OverrideEvent {self.action} {self.field}>"


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


class FixedAssetRegisterItem(db.Model):
    """One asset, read out of a client's fixed asset register.

    Nothing downstream can check depreciation against an asset's useful life
    from a trial balance alone - a trial balance carries one net book value
    per class of asset, not what any individual asset cost, when it was
    bought, or how long it is expected to last. This is that missing
    per-asset detail, read once from the register the client actually sent.

    Stored per financial year, like a trial balance account: the same asset
    reappears in next year's register, and each year's figures are read from
    that year's own upload rather than carried forward and drifting from it.
    """

    __tablename__ = "fixed_asset_register_items"

    id = db.Column(db.Integer, primary_key=True)
    financial_year_id = db.Column(db.Integer,
                                  db.ForeignKey("financial_years.id"),
                                  nullable=False, index=True)
    source_document_id = db.Column(db.Integer, db.ForeignKey("documents.id"))

    description = db.Column(db.String(255), nullable=False)
    cost = db.Column(Numeric(18, 2), nullable=False)
    purchase_date = db.Column(db.Date)
    # Null for an asset still held at the year end.
    disposal_date = db.Column(db.Date)
    useful_life_years = db.Column(db.Float, nullable=False)

    confidence = db.Column(db.Float, default=1.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    financial_year = db.relationship("FinancialYear",
                                     back_populates="fixed_asset_register_items")
    source_document = db.relationship("Document")

    def __repr__(self):
        return f"<FixedAssetRegisterItem {self.description!r}>"


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


# Wording the FRS library leaves for the firm to fill in. Five phrases in the
# notes carry a blank - "Trade receivables are generally granted credit terms
# of {credit_terms_receivable}" - because the answer is a policy, not a
# figure, and no trial balance holds it.
#
# Each is a firm-wide default that a single client can depart from: most of a
# firm's clients are on the same terms, and the one that is not should not
# force the other twenty to be set individually. See DisclosureSetting.
DISCLOSURE_SETTINGS = (
    ("credit_terms_receivable",
     "Credit terms given to customers",
     "e.g. 30 to 60 days",
     "Appears in the Trade and other receivables note."),
    ("credit_terms_payable",
     "Credit terms received from suppliers",
     "e.g. 30 days",
     "Appears in the Trade and other payables note."),
    ("sicr_days",
     "Days overdue before credit risk has increased significantly",
     "e.g. 30 days",
     "The point at which a receivable is no longer considered low risk."),
    ("default_days",
     "Days overdue before a receivable is in default",
     "e.g. 90 days",
     "Appears in the Credit risk note."),
    ("writeoff_days",
     "Days overdue before a receivable is written off",
     "e.g. 365 days",
     "The point at which recovery is no longer considered likely."),
)

# The practitioner's particulars, which SSRS 4410 needs on a compilation
# report. Firm-wide, so they live with the other standing wording.
DISCLOSURE_SETTINGS = DISCLOSURE_SETTINGS + (
    ("practitioner_name",
     "Practitioner or firm name (compilation report)",
     "e.g. AltiusNXT Public Accountants",
     "Signs the compilation report."),
    ("practitioner_address",
     "Practitioner address (compilation report)",
     "e.g. 1 Example Road, Singapore 123456",
     "Printed under the practitioner's name."),
)

DISCLOSURE_SETTING_KEYS = {key for key, _l, _p, _h in DISCLOSURE_SETTINGS}


class DisclosureSetting(db.Model):
    """One piece of standing wording the notes need and no figure supplies.

    ONE TABLE FOR BOTH LEVELS. `customer_id` NULL is the firm's own default,
    applying to every engagement; a row with a customer is that client
    departing from it. Kept as one table rather than a firm table plus client
    columns because the fallback is then a single ordered lookup, and adding
    a sixth setting is a row rather than a migration on two tables.

    Values are free text, not numbers, deliberately. "30 to 60 days" and
    "30 days from invoice date" are both real answers a firm gives, and
    storing 30 would force the note to invent the rest of the sentence.
    """

    __tablename__ = "disclosure_settings"
    __table_args__ = (
        db.UniqueConstraint("customer_id", "key",
                            name="uq_disclosure_setting_scope_key"),
    )

    id = db.Column(db.Integer, primary_key=True)
    # NULL = the firm's default. Set = this client's own answer.
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"),
                            index=True)
    key = db.Column(db.String(60), nullable=False, index=True)
    value = db.Column(db.Text, nullable=False)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"))

    def __repr__(self):
        scope = f"customer {self.customer_id}" if self.customer_id else "firm"
        return f"<DisclosureSetting {self.key} ({scope})>"

class CashFlowEntry(db.Model):
    """One line of the statement of cash flows, as the preparer entered it.

    The client's standard lines (v8) make the cash flow the preparer's entry,
    not a derivation: no plug line, held incomplete until it is entered and
    closing cash agrees to the balance sheet. The line is identified by its
    caption (normalised, "#2" for a repeated one) because the captions are the
    template's own and that is all a template row has to hold on to.
    """

    __tablename__ = "cash_flow_entries"
    __table_args__ = (
        db.UniqueConstraint("financial_year_id", "row_key",
                            name="uq_cash_flow_entry"),
    )

    id = db.Column(db.Integer, primary_key=True)
    financial_year_id = db.Column(db.Integer, db.ForeignKey("financial_years.id"),
                                  nullable=False, index=True)
    row_key = db.Column(db.String(200), nullable=False)
    label = db.Column(db.String(255))
    amount = db.Column(Numeric(18, 2), nullable=False, default=0)
    entered_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<CashFlowEntry fy={self.financial_year_id} {self.row_key}={self.amount}>"


class AiUsage(db.Model):
    """One request to the language model: what for, for whom, how many tokens.

    Counts and labels only - never a document's text or the model's reply. The
    ids are plain integers, not foreign keys, so a usage row survives the
    deletion of the client or document it was for.
    """

    __tablename__ = "ai_usage"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False,
                           index=True)
    purpose = db.Column(db.String(30), nullable=False, index=True)
    provider = db.Column(db.String(20))
    model = db.Column(db.String(80))
    customer_id = db.Column(db.Integer, index=True)
    financial_year_id = db.Column(db.Integer, index=True)
    document_id = db.Column(db.Integer)
    user_id = db.Column(db.Integer)
    input_tokens = db.Column(db.Integer)
    output_tokens = db.Column(db.Integer)
    total_tokens = db.Column(db.Integer)
    seconds = db.Column(db.Float)
    ok = db.Column(db.Boolean, default=True, nullable=False)
    error = db.Column(db.String(300))

    def __repr__(self):
        return f"<AiUsage {self.purpose} {self.total_tokens}>"
