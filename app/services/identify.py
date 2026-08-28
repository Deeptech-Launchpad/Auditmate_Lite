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
from decimal import Decimal

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

# A trial balance states each account as a debit or a credit. A printed
# statement states one signed amount. Extraction preserves that difference,
# so the proportion of rows carrying a debit or credit separates them.
PAIRED_MIN = 0.60

# Below this there is not enough document to read.
MIN_ROWS = 5


def _signals(rows, customer_id):
    """Measure the four things that tell these documents apart."""
    total = paired = mapped = 0
    groups = set()

    for row in rows:
        label = (row.label or "").strip()
        if not label or looks_like_total_label(label):
            continue

        total += 1
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


def identify_document(document):
    """Set a document's category from its contents, unless a human set it.

    Returns (category, reason, changed). Called at the end of extraction,
    where the rows exist for the first time.
    """
    from ..models import ExtractedLineItem

    if document.category_source == "manual":
        return document.category, "set by the auditor", False

    rows = (ExtractedLineItem.query
            .filter_by(document_id=document.id)
            .filter(ExtractedLineItem.status != "discarded")
            .all())

    customer_id = document.financial_year.customer_id
    category, reason = identify(rows, customer_id)

    if category is None:
        # The contents did not settle it, so whatever the file name decided
        # stands. Saying nothing is better than overwriting a reasonable
        # guess with a worse one.
        log.info("Document %s: contents inconclusive (%s)", document.id, reason)
        return document.category, reason, False

    changed = category != document.category
    document.category = category
    document.category_source = "content"
    document.category_reason = reason[:255]
    log.info("Document %s: identified as %s (%s)", document.id, category, reason)
    return category, reason, changed
