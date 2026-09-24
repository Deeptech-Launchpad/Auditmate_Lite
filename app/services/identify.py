"""Working out what a document is from what is inside it.

A file name is a label somebody typed. `Report_final_v2.pdf` says nothing,
and a client who renames an export defeats name matching entirely. The
contents cannot be renamed: a trial balance, a balance sheet, a profit and
loss and a general ledger are structurally different documents, and the
difference is visible in the rows we have already extracted.

    trial balance    debit and credit columns; assets AND income
    balance sheet    one amount column; assets, liabilities, equity, no income
    profit and loss  one amount column; income and expenses, no assets
    general ledger   hundreds or thousands of rows, most naming a supplier
                     rather than an account

This decides only those four, because those four are what the accounts are
built from or checked against line for line, and those four are what the
structure can actually prove. An aged receivables listing and a bank
statement are better recognised by their names - see categorise.py - and a
guess there would be a guess, not a reading.

Order matters in the test below. A general ledger also carries debit and
credit columns, so it has to be ruled out before that signal is trusted.

Never authoritative. What it decides is shown in the documents list and the
auditor can change it, because the category chooses which document the
accounts are built FROM - see models.TB_SOURCE_PRECEDENCE - and a wrong
source is worse than an unset one. An auditor's own choice is never
overruled.
"""
import logging
import re
from datetime import date, timedelta
from decimal import Decimal

from ..models import PRIOR_YEAR_TWIN
from .classify import classify
from .extraction.base import looks_like_total_label
from .mapping import match_label

log = logging.getLogger(__name__)

# Which side of the accounts a mapped line sits on. Taken from the `group:`
# field in statement_templates.yaml, so this list and the statements cannot
# drift apart without the templates changing too.
BALANCE_SHEET_GROUPS = {
    "current_assets", "non_current_assets", "assets_total",
    "current_liabilities", "non_current_liabilities", "liabilities_total",
    "equity",
}

PROFIT_LOSS_GROUPS = {
    "revenue", "cost_of_sales", "gross", "operating_expenses",
    "operating_after", "result",
}

# A statement has one line per account. A ledger has one line per
# transaction, so it is an order of magnitude longer than anything else a
# client sends. The smallest real ledger seen so far ran to 3,005 rows; the
# largest trial balance, 90.
LEDGER_MIN_ROWS = 250

# And most of a ledger's rows name a supplier, a customer or an invoice
# rather than an account, so they match no mapping rule. On a real
# engagement 2,308 of 2,339 ledger rows matched nothing at all.
LEDGER_MAX_MAPPED = 0.35

# The mapped share alone missed real ledgers. A ledger lists the same
# account again for every transaction - "UOB Bank" 540 times in one of
# 2,121 rows - so its distinct labels are a small fraction of its rows (0.30
# to 0.32 on the two seen), while a trial balance names each account about
# once (0.50, from an account listed under both its income and balance
# sides). Those ledgers matched 54% and 57% of their rows against account
# names, cleared the mapped test above, and were filed as trial balances.
LEDGER_MAX_DISTINCT = 0.60

# A trial balance states each account as a debit or a credit. A printed
# statement states one signed amount. Extraction preserves that difference,
# so the proportion of rows carrying a debit or credit separates them.
PAIRED_MIN = 0.60

# Below this there is not enough document to read.
MIN_ROWS = 5

# The four this module can actually tell apart, plus the two states that mean
# nothing has been decided yet. A category OUTSIDE this set was reached some
# other way - a file name that said "Cash Flow Statement", or an auditor - and
# must not be overwritten here, because the tests below cannot distinguish the
# thing it already is. A cash flow statement has no debit and credit columns
# and its lines touch both sides of the accounts, so it falls into the
# catch-all and is filed as a trial balance - which then BUILDS the accounts
# out of movements. Silence is the only honest answer for a document this
# module was never taught to read.
DECIDABLE = {
    "trial_balance", "balance_sheet", "profit_and_loss", "general_ledger",
    "other", None,
}


def _signals(rows, customer_id):
    """Measure the four things that tell these documents apart."""
    total = paired = mapped = 0
    groups = set()
    labels = set()

    for row in rows:
        label = (row.label or "").strip()
        if not label or looks_like_total_label(label):
            continue

        total += 1
        labels.add(label.lower())
        if row.debit is not None or row.credit is not None:
            paired += 1

        rule = match_label(label, customer_id)
        if not rule:
            continue
        mapped += 1
        entry = classify(rule["line_key"])
        if entry and entry.get("group"):
            groups.add(entry["group"])

    if not total:
        return None

    return {
        "rows": total,
        "paired": paired / total,
        "mapped": mapped / total,
        "distinct": len(labels) / total,
        "balance_sheet": bool(groups & BALANCE_SHEET_GROUPS),
        "profit_loss": bool(groups & PROFIT_LOSS_GROUPS),
    }


def identify(rows, customer_id):
    """What these extracted rows say the document is.

    Returns (category, reason) - or (None, reason) when the contents do not
    settle it, which is an honest answer and leaves the file name's guess
    standing.
    """
    if len(rows) < MIN_ROWS:
        return None, f"only {len(rows)} row(s) - too little to read"

    s = _signals(rows, customer_id)
    if s is None:
        return None, "no readable rows"

    # A ledger first. It carries debit and credit columns like a trial
    # balance, so testing for those before ruling it out would file every
    # ledger as a trial balance - and a ledger used as the source produces
    # hundreds of accounts named after suppliers.
    if s["rows"] >= LEDGER_MIN_ROWS and s["mapped"] < LEDGER_MAX_MAPPED:
        return "general_ledger", (
            f"{s['rows']} rows and only {s['mapped']:.0%} match an account "
            f"name - transactions, not balances")
    if s["rows"] >= LEDGER_MIN_ROWS and s["distinct"] < LEDGER_MAX_DISTINCT:
        return "general_ledger", (
            f"{s['rows']} rows but only {s['distinct']:.0%} are different "
            f"names - the same accounts repeated, which is transactions, "
            f"not balances")

    if s["paired"] >= PAIRED_MIN:
        return "trial_balance", (
            f"{s['paired']:.0%} of rows carry a debit or a credit")

    if s["balance_sheet"] and not s["profit_loss"]:
        return "balance_sheet", (
            "assets, liabilities and equity, and no income or expenses")

    if s["profit_loss"] and not s["balance_sheet"]:
        return "profit_and_loss", (
            "income and expenses, and no assets or liabilities")

    if s["balance_sheet"] and s["profit_loss"]:
        return "trial_balance", (
            "every kind of account in one document")

    return None, (
        f"{s['rows']} rows, {s['mapped']:.0%} recognised - nothing decisive")


# A set of signed accounts is a long document that carries the statements AND
# the notes to them. Rows cannot tell it from a trial balance - a face
# statement holds assets and income both - so it is recognised by what it
# says, which no other document in this list says.
SIGNED_MIN_PAGES = 8


def looks_like_signed_accounts(raw_text, page_count):
    text = " ".join((raw_text or "").lower().split())
    if not text or not page_count or page_count < SIGNED_MIN_PAGES:
        return False
    has_notes = ("notes to the financial statements" in text
                 or "notes to the accounts" in text)
    has_position = ("statement of financial position" in text
                    or "balance sheet" in text)
    has_result = ("profit or loss" in text or "comprehensive income" in text
                  or "income statement" in text)
    return has_notes and has_position and has_result


# The date a statement is made up to, as its own title states it: "For the
# year ended 31 December 2024", "As at 31 Dec 2024". Spaces are optional
# because a PDF's text layer often has none ("Fortheyearended31December2024").
# Only the title phrase is read - a column heading like "31 DEC 2024" also
# appears on a current-year balance sheet beside its comparative, and reading
# those would file this year's own document as last year's.
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_PERIOD_END = re.compile(
    r"(?:year\s*ended|period\s*ended|years\s*ended|as\s*at|as\s*of|"
    r"financial\s*year\s*ended)\s*(\d{1,2})\s*([A-Za-z]{3,9})\s*(\d{4})",
    re.IGNORECASE)


def detect_period_end(raw_text):
    """The date a document says it runs to, or None."""
    for day, month, year in _PERIOD_END.findall((raw_text or "")[:2500]):
        number = _MONTHS.get(month[:3].lower())
        if not number:
            continue
        try:
            return date(int(year), number, int(day))
        except ValueError:
            continue
    return None


def _describes_last_year(document, raw_text):
    """The period the document states, if that is the year BEFORE this one.

    A client sends last year's profit and loss or trial balance beside this
    year's, and nothing in a file NAME says which is which. The document
    itself does: it states the year it is made up to. Left to the Year box
    the wrong year's figures count as this year's - the review screen then
    showed last year's profit and loss, titled "for the year ended 31
    December 2024", with the 2024 column as this year and asked whether "the
    years are the wrong way round".
    """
    stated = detect_period_end(raw_text)
    if stated is None:
        return None
    financial_year = document.financial_year
    previous = getattr(financial_year, "previous_year", None)
    last_end = (previous.end_date if previous is not None
                else financial_year.start_date - timedelta(days=1))
    if abs((stated - last_end).days) <= 5 and             abs((stated - financial_year.end_date).days) > 5:
        return stated
    return None


def _identify_document(document, raw_text="", page_count=None):
    """Set a document's category from its contents, unless a human set it.

    Returns (category, reason, changed). Called at the end of extraction,
    where the rows exist for the first time. `raw_text` and `page_count`
    are the document's own text and length, for the one kind of document
    the rows alone cannot recognise - see looks_like_signed_accounts.
    """
    from ..models import ExtractedLineItem

    if document.category_source == "manual":
        return document.category, "set by the auditor", False

    if document.category not in DECIDABLE:
        return (document.category,
                "already a category the contents cannot argue with", False)

    rows = (ExtractedLineItem.query
            .filter_by(document_id=document.id)
            .filter(ExtractedLineItem.status != "discarded")
            .all())

    customer_id = document.financial_year.customer_id
    if looks_like_signed_accounts(raw_text, page_count):
        category, reason = ("signed_accounts",
                            f"{page_count} pages with the statements and the "
                            f"notes to them - a set of signed accounts")
    else:
        category, reason = identify(rows, customer_id)

    if category is None:
        # The contents did not settle it, so whatever the file name decided
        # stands. Saying nothing is better than overwriting a reasonable
        # guess with a worse one - though the year the document states is
        # still worth acting on.
        log.info("Document %s: contents inconclusive (%s)", document.id, reason)
        if (document.category in PRIOR_YEAR_TWIN
                and _describes_last_year(document, raw_text) is not None):
            category = document.category
        else:
            return document.category, reason, False

    # Which year it describes. Only for a current-year category: a prior-year
    # one was already chosen by a person or by an earlier pass.
    last_year = _describes_last_year(document, raw_text)
    if last_year is not None and category in PRIOR_YEAR_TWIN             and category not in {"signed_accounts"}:
        category = PRIOR_YEAR_TWIN[category]
        reason = (f"{reason}; it is made up to {last_year:%d %B %Y}, which "
                  f"is last year, so it is filed as the prior-year version")

    changed = category != document.category
    document.category = category
    document.category_source = "content"
    document.category_reason = reason[:255]
    log.info("Document %s: identified as %s (%s)", document.id, category, reason)
    return category, reason, changed


# Documents that are themselves LAST year's. Their first column is the year
# that counts here; the second is the year before that, which nothing in this
# engagement reads - so it is neither shown nor allowed to hold up review.
LAST_YEAR_CATEGORIES = set(PRIOR_YEAR_TWIN.values()) | {"signed_accounts"}


def is_last_year_document(document):
    return document.category in LAST_YEAR_CATEGORIES


def clear_unused_year_flags(document):
    """Stop the year-before rows of a last-year document needing review.

    They score low like any row, and the review screen listed each account
    once with both years - so a preparer accepted the six rows they could
    see and was told six more still needed checking, rows they had no cell
    to accept and no reason to look at. Returns how many were cleared.
    """
    from ..extensions import db
    from ..models import ExtractedLineItem

    if not is_last_year_document(document):
        return 0
    cleared = (ExtractedLineItem.query
               .filter_by(document_id=document.id, period="previous",
                          needs_review=True, status="auto")
               .update({"needs_review": False}))
    if cleared:
        db.session.flush()
    return cleared


def identify_document(document, raw_text="", page_count=None):
    """See _identify_document. Then clears the flags on a last-year
    document's unused year-before column."""
    outcome = _identify_document(document, raw_text=raw_text,
                                 page_count=page_count)
    clear_unused_year_flags(document)
    return outcome
