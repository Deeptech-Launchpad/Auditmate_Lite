"""Last year's closing balances, and how confident we are in them.

Last year's figures are needed four times over, and it is easy to file the
document that carries them as "evidence" and miss half of what it is for:

    1. The comparative column of every statement   - required DATA
    2. Opening balances agreeing to last year's    - a check
    3. The movement review                         - a check
    4. Last year's mapping                         - required DATA

Jobs 1 and 4 mean the statements cannot be issued at all without this. It is
not a check that might raise a difference; it is an input.

There are three places the figures can come from, and they are NOT
interchangeable:

    AUDITMATE   the previous engagement, done here and approved. Exact, and
                already mapped to statement lines.
    XERO        pulled at the prior year end. Exact, no reading, no AI - but
                it is what the accounting system says TODAY, which is not
                necessarily what was signed.
    SIGNED      last year's signed accounts, read from a PDF. What was
                actually reported and filed. Authoritative in the sense that
                matters, but read by a parser and therefore fallible.

That distinction is the whole point of job 2. Xero says what the books hold
now; the signed accounts say what was reported. **When both exist and they
disagree, somebody has posted into a year that was already signed off** -
and no amount of checking this year's trial balance against itself would
ever reveal it.

So this module does not pick one source and discard the rest. It resolves a
best set of figures for the jobs that need data, and separately compares the
sources against each other for the job that is a check.
"""
import logging
from decimal import Decimal

from ..models import Document, ExtractedLineItem, TrialBalanceAccount
from .classify import classify, is_credit_balance
from .extraction.base import looks_like_total_label
from .mapping import match_label
from .outward import previous_year

log = logging.getLogger(__name__)

ZERO = Decimal("0.00")

# Statements are presented to the dollar, a trial balance carries cents.
TOLERANCE = Decimal("1.00")

# Best first, for the jobs that need figures rather than a comparison.
# Auditmate's own previous engagement outranks everything: it was mapped and
# approved here, so it needs no interpretation at all.
SOURCE_ORDER = ["auditmate", "signed_accounts", "tb_comparative", "xero",
                "prior_trial_balance"]

SOURCE_LABELS = {
    "auditmate": "last year's engagement in Auditmate",
    "signed_accounts": "last year's signed accounts",
    "tb_comparative": "the prior-year column of this year's trial balance",
    "xero": "Xero, at last year's year end",
    "prior_trial_balance": "last year's trial balance, uploaded and marked "
                           "as the previous year",
}


def _net(account) -> Decimal:
    return (account.debit or ZERO) - (account.credit or ZERO)


def _from_auditmate(financial_year):
    """The previous engagement's approved trial balance, by statement line."""
    previous = previous_year(financial_year)
    if previous is None:
        return None

    rows = (TrialBalanceAccount.query
            .filter_by(financial_year_id=previous.id)
            .filter(TrialBalanceAccount.standard_key.isnot(None))
            .all())
    if not rows:
        return None

    totals = {}
    for row in rows:
        totals[row.standard_key] = totals.get(row.standard_key, ZERO) + _net(row)
    return totals


def _from_document(document, customer_id):
    """One document's figures, mapped to statement lines.

    Only the document's CURRENT-year column is read. Last year's signed
    accounts print two years side by side, and its prior-year column is the
    year before last - which belongs nowhere in this engagement. Extraction
    marks those rows "previous"; reading them would shift every comparative
    back by a year while looking perfectly reasonable.
    """
    rows = (ExtractedLineItem.query
            .filter_by(document_id=document.id)
            .filter(ExtractedLineItem.status != "discarded")
            .all())

    totals = {}
    for row in rows:
        if row.period == "previous":
            continue
        label = (row.label or "").strip()
        if not label or looks_like_total_label(label):
            continue

        rule = match_label(label, customer_id)
        if not rule:
            continue

        key = rule["line_key"]

        if row.debit is not None or row.credit is not None:
            amount = (Decimal(str(row.debit or 0))
                      - Decimal(str(row.credit or 0)))
        elif row.amount is not None:
            # A printed statement has one amount column and no sides, so
            # payables, share capital and revenue are all shown as positive
            # numbers. Everything else here is debit-positive, and comparing
            # the two conventions would report a liability of 20,000 against
            # one of 23,000 as a difference of 43,000.
            amount = Decimal(str(row.amount))
            on_credit = is_credit_balance(key)
            if amount < 0:
                amount, on_credit = -amount, not on_credit
            amount = -amount if on_credit else amount
        else:
            continue
        totals[key] = totals.get(key, ZERO) + amount

    return totals or None


def _document_of(financial_year, category, file_type=None):
    for document in financial_year.documents:
        if document.category != category:
            continue
        if file_type and document.file_type != file_type:
            continue
        if document.review_status == "verified" or file_type == "xero_prior":
            return document
    return None


def _from_tb_comparative(financial_year):
    """Last year, off the accounts this year's trial balance built.

    Read through the ACCOUNT rather than the document, so the comparative
    follows the same mapping as the current figure. Re-map an account by hand
    and last year moves with it; read from the document instead and the two
    columns would quietly disagree.

    The sign flip is the part easy to leave out and wrong every time it is:
    a trial balance stores raw debits and credits, and a credit-balance
    account - revenue, every liability - is negative in that form. The
    CURRENT figure is flipped to a presentation sign by the account's own
    mapping rule (see statements._signed). Skipping that flip here would
    print revenue as a positive this year and a negative last year on the
    same line - not merely wrong, but wrong in the way that is obvious to
    anyone who opens the document, on the client's largest number.
    """
    rows = (TrialBalanceAccount.query
            .filter_by(financial_year_id=financial_year.id)
            .filter(TrialBalanceAccount.standard_key.isnot(None))
            .all())

    totals = {}
    for row in rows:
        if row.prior_debit is None and row.prior_credit is None:
            continue                      # no comparative for this account
        net = (row.prior_debit or ZERO) - (row.prior_credit or ZERO)
        rule = match_label(row.account_name, financial_year.customer_id)
        sign = rule["sign"] if rule else 1
        totals[row.standard_key] = totals.get(row.standard_key, ZERO) + net * sign
    return totals


def sources(financial_year):
    """Every prior-year source available, as {name: {key: amount}}.

    A source that is present but empty is left out rather than recorded as a
    set of zeroes - "we have the document but could read nothing from it" is
    not the same as "last year was nil", and treating it as the latter would
    put false differences in front of an auditor.
    """
    found = {}

    from_app = _from_auditmate(financial_year)
    if from_app:
        found["auditmate"] = from_app

    signed = _document_of(financial_year, "signed_accounts")
    if signed is not None:
        figures = _from_document(signed, financial_year.customer_id)
        if figures:
            found["signed_accounts"] = figures

    # This year's own trial balance prints last year beside it, and the
    # build now keeps that column on the account. Ranked below the signed
    # accounts, which are the figures that were actually filed, and above a
    # Xero pull, which is a second export of the same books.
    from_tb = _from_tb_comparative(financial_year)
    if from_tb:
        found["tb_comparative"] = from_tb

    pulled = _document_of(financial_year, "prior_trial_balance",
                          file_type="xero_prior")
    if pulled is not None:
        figures = _from_document(pulled, financial_year.customer_id)
        if figures:
            found["xero"] = figures

    # The same thing as a file: last year's trial balance, uploaded and marked
    # "Previous year" beside its category. Until this was read, choosing that
    # year on the Documents screen filed the document correctly and then did
    # nothing with it - the figures had nowhere to land.
    uploaded = _document_of(financial_year, "prior_trial_balance")
    if uploaded is not None and uploaded is not pulled:
        figures = _from_document(uploaded, financial_year.customer_id)
        if figures:
            found["prior_trial_balance"] = figures

    return found


def balances(financial_year):
    """Last year's figures for the jobs that need data, and where from.

    Returns (figures, source_name) - or ({}, None) when last year is simply
    not available, which is the honest answer and the one the readiness check
    reports rather than papering over.
    """
    available = sources(financial_year)
    for name in SOURCE_ORDER:
        if name in available:
            return available[name], name
    return {}, None


# Sources that are a TRIAL BALANCE rather than a finished set of accounts.
# The distinction matters for one account only, and it matters a lot.
TRIAL_BALANCE_SOURCES = {"auditmate", "xero", "tb_comparative",
                         "prior_trial_balance"}

# Where the year's result lands once it is appropriated.
RESULT_KEYS = ["retained_earnings", "accumulated_profit"]


def _fold_result(totals):
    """Move the year's result into retained earnings, as a balance sheet does.

    A year-end trial balance shows retained earnings at its OPENING value,
    with the year's profit still sitting across revenue and expenses. A
    signed balance sheet shows it after appropriation - opening plus the
    result. The two are both correct and they are not the same number.

    Compared unadjusted, every engagement would report a difference on
    retained earnings exactly the size of the year's profit, every time. A
    check that cries wolf on a real client's largest equity balance is worse
    than no check: it teaches the auditor to skip the panel.

    Working in debit-positive terms, revenue is negative and expenses are
    positive, so the P&L total IS the negated result - which is the sign
    retained earnings already carries. Adding it is the whole adjustment.
    """
    adjusted = dict(totals)
    result = ZERO
    for key, amount in totals.items():
        entry = classify(key)
        if entry and entry.get("fs") == "P&L":
            result += amount

    if result == ZERO:
        return adjusted

    target = next((k for k in RESULT_KEYS if k in adjusted), RESULT_KEYS[0])
    adjusted[target] = adjusted.get(target, ZERO) + result
    return adjusted


def opening_check(financial_year):
    """Do the books still agree with what was signed?

    This is the check the firm's own example points at: a tax payable
    balance that had not moved for a full year, sitting in accounts that
    balanced perfectly. Comparing this year's trial balance against itself
    finds nothing. Comparing what the accounting system says last year closed
    at against what was actually signed and filed finds it immediately.

    Needs two independent sources. With only one there is nothing to compare,
    and saying so is more use than showing an empty table.
    """
    available = {
        name: (_fold_result(figures) if name in TRIAL_BALANCE_SOURCES
               else figures)
        for name, figures in sources(financial_year).items()}

    if len(available) < 2:
        return {
            "comparable": False,
            "have": [SOURCE_LABELS[n] for n in available],
            "rows": [], "differs": 0, "missing": 0,
        }

    # Signed accounts are the reference wherever they exist: they are what
    # was reported. Otherwise Auditmate's own previous engagement stands in.
    if "signed_accounts" in available:
        reference = "signed_accounts"
    else:
        reference = next(n for n in SOURCE_ORDER if n in available)
    compared = [n for n in SOURCE_ORDER if n in available and n != reference]

    rows = []
    for other in compared:
        left, right = available[reference], available[other]
        for key in sorted(set(left) | set(right)):
            entry = classify(key)
            # A check on the balance sheet only. An income or expense account
            # starts every year at nil by definition, so "it moved" says
            # nothing - it is supposed to.
            if not entry or entry.get("fs") != "Balance Sheet":
                continue

            signed_amount = left.get(key)
            other_amount = right.get(key)

            if signed_amount is None or other_amount is None:
                status = "missing"
                difference = signed_amount if other_amount is None else other_amount
            else:
                difference = other_amount - signed_amount
                status = ("agrees" if abs(difference) <= TOLERANCE
                          else "differs")

            if status == "agrees":
                continue          # only the exceptions are worth the space

            rows.append({
                "key": key,
                "label": entry["label"],
                "reference": signed_amount,
                "other": other_amount,
                "difference": difference,
                "status": status,
                "against": SOURCE_LABELS[other],
            })

    return {
        "comparable": True,
        "reference": SOURCE_LABELS[reference],
        "compared": [SOURCE_LABELS[n] for n in compared],
        "have": [SOURCE_LABELS[n] for n in available],
        "rows": rows,
        "differs": sum(1 for r in rows if r["status"] == "differs"),
        "missing": sum(1 for r in rows if r["status"] == "missing"),
    }
