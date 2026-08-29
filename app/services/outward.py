"""Checking the trial balance against everything that is not the trial balance.

v1 asked whether the debits equalled the credits. Xero cannot post a
one-sided entry, so that was never information. On the engagement the firm
tested, income tax and GST sat in operating expenses, depreciation did not
agree to the asset's useful life, and tax payable had not moved in a year -
and every one of those balanced perfectly.

Errors are found by comparing the trial balance with sources outside it.
This module holds two kinds of comparison that reconcile.py does not:

    A DOCUMENT'S OWN TOTAL against the line it should equal. An aged
    receivables listing footing 189,432 against trade receivables of 180,432
    is a finding; a bank statement closing at a figure the trial balance
    does not carry is a finding.

    LAST YEAR against this year, line by line. A balance that has not moved
    in twelve months, one that has moved several times over, an account that
    has appeared and one that has vanished. None of these is necessarily
    wrong. All of them are questions worth asking, and none of them is
    visible in a total.

reconcile.py stays as it is: it compares a client's own statement line for
line against ours, which is a different question and still the right one for
a balance sheet or a profit and loss.
"""
import logging
from decimal import Decimal

from ..extensions import db
from ..models import FinancialYear, TrialBalanceAccount
from .classify import classify
from .extraction.base import looks_like_total_label

log = logging.getLogger(__name__)

ZERO = Decimal("0.00")

# Statements are presented to the dollar while a trial balance carries
# cents, so an exact match would report rounding as a finding and bury the
# real ones.
TOLERANCE = Decimal("1.00")

# A balance that changes by more than this is worth a second look. Not a
# fault - a growing company moves - but it is where cut-off errors and
# misposted entries show up, so it deserves to be named rather than passed
# over.
LARGE_MOVEMENT = Decimal("0.50")      # 50%

# Accounts where standing perfectly still is normal, not a question. Share
# capital does not move unless shares were issued, and flagging it every year
# is how a panel of real findings gets skipped.
EXPECTED_STATIC = {"share_capital", "working_capital"}

# And accounts where moving is the definition. Retained earnings changes by
# the year's result; reporting that as a sharp movement says only that the
# company traded.
EXPECTED_TO_MOVE = {"retained_earnings", "accumulated_profit"}

# Which evidence document should agree with which statement line.
#
# The right-hand side is a list because a client's chart of accounts splits
# these differently: cash may sit in one account or in five, and all of them
# together are what a bank statement is evidence for.
EVIDENCE_LINES = {
    "bank_statement": {
        "label": "Bank statement",
        "keys": ["cash_and_equivalents"],
        "line": "Cash and cash equivalents",
    },
    "receivables": {
        "label": "Aged receivables listing",
        "keys": ["trade_receivables"],
        "line": "Trade receivables",
    },
    "payables": {
        "label": "Aged payables listing",
        "keys": ["trade_payables"],
        "line": "Trade payables",
    },
    "fixed_asset_register": {
        "label": "Fixed asset register",
        "keys": ["ppe"],
        "line": "Property, plant and equipment",
    },
    "tax_document": {
        "label": "Tax document",
        "keys": ["tax_payable", "tax_expense"],
        "line": "Tax",
    },
}


def _net(account) -> Decimal:
    return (account.debit or ZERO) - (account.credit or ZERO)


def _by_key(financial_year_id):
    """standard_key -> the trial balance's total for that line."""
    totals = {}
    for account in (TrialBalanceAccount.query
                    .filter_by(financial_year_id=financial_year_id).all()):
        if not account.standard_key:
            continue
        totals[account.standard_key] = (totals.get(account.standard_key, ZERO)
                                        + _net(account))
    return totals


def _document_total(document):
    """What a listing or a statement foots to.

    A document that prints its own total is taken at its word - that figure
    is the client's assertion and is exactly what should be held against
    ours. Only when there is no total row do the rows get added up, because
    adding rows to a document that already totals them would double it.
    """
    rows = [r for r in document.line_items if r.status != "discarded"]
    rows = [r for r in rows
            if r.period is None or r.period != "previous"]
    if not rows:
        return None, 0

    def value(row):
        if row.debit is not None or row.credit is not None:
            return (Decimal(str(row.debit or 0)) - Decimal(str(row.credit or 0)))
        return Decimal(str(row.amount)) if row.amount is not None else ZERO

    totals = [r for r in rows if looks_like_total_label(r.label or "")]
    if totals:
        # The last total on a listing is the grand total; earlier ones are
        # section subtotals, and summing them would count the document twice.
        stated = value(totals[-1])
        if stated != ZERO:
            return stated, len(rows)

    body = [r for r in rows if not looks_like_total_label(r.label or "")]
    if not body:
        return None, len(rows)
    return sum((value(r) for r in body), ZERO), len(rows)


def evidence_checks(financial_year):
    """Each evidence document's own total against the line it should equal."""
    ours = _by_key(financial_year.id)
    if not ours:
        return []

    findings = []
    for document in financial_year.documents:
        if document.review_status != "verified":
            continue
        spec = EVIDENCE_LINES.get(document.category or "")
        if not spec:
            continue

        theirs, rows = _document_total(document)
        if theirs is None:
            continue

        present = [k for k in spec["keys"] if k in ours]
        if not present:
            findings.append({
                "document": document,
                "kind": spec["label"],
                "line": spec["line"],
                "theirs": theirs,
                "ours": None,
                "difference": theirs,
                "rows": rows,
                "status": "missing",
                "note": (f"The trial balance has no {spec['line'].lower()} "
                         f"at all, and this document states one."),
            })
            continue

        mine = sum((ours[k] for k in present), ZERO)
        # A listing prints positive figures; the trial balance holds
        # liabilities as credits. Compare magnitudes.
        difference = abs(mine) - abs(theirs)
        findings.append({
            "document": document,
            "kind": spec["label"],
            "line": spec["line"],
            "theirs": theirs,
            "ours": mine,
            "difference": difference,
            "rows": rows,
            "status": "agrees" if abs(difference) <= TOLERANCE else "differs",
            "note": None,
        })

    return findings


def previous_year(financial_year):
    """The engagement immediately before this one, for the same client.

    An explicit link wins. FinancialYear.previous_year_id is set by whoever
    created the engagement and says which year this one follows - which is
    the answer, not an inference from dates. Only when it is absent does the
    most recent earlier year end stand in, and a year with no end date
    cannot be ordered at all so it is left out rather than guessed at.
    """
    if financial_year.previous_year_id:
        linked = db.session.get(FinancialYear, financial_year.previous_year_id)
        if linked is not None:
            return linked

    if financial_year.end_date is None:
        return None

    return (FinancialYear.query
            .filter(FinancialYear.customer_id == financial_year.customer_id,
                    FinancialYear.id != financial_year.id,
                    FinancialYear.end_date.isnot(None),
                    FinancialYear.end_date < financial_year.end_date)
            .order_by(FinancialYear.end_date.desc())
            .first())


def movements(financial_year):
    """This year against last year, line by line.

    Returns None when there is no previous engagement in the app - which is
    also the answer to "why is there no comparative column", and is worth
    saying rather than showing an empty table.
    """
    previous = previous_year(financial_year)
    if previous is None:
        return None

    now = _by_key(financial_year.id)
    then = _by_key(previous.id)
    if not now and not then:
        return None

    rows = []
    for key in sorted(set(now) | set(then)):
        entry = classify(key)
        label = entry["label"] if entry else key
        this_year = now.get(key)
        last_year = then.get(key)

        if last_year is None:
            status, note = "new", "Not in last year's accounts."
        elif this_year is None:
            status, note = "gone", "In last year's accounts, absent now."
        elif this_year == last_year and this_year != ZERO:
            if key in EXPECTED_STATIC:
                status, note = "normal", None
            else:
                status, note = "unchanged", (
                    "Identical to last year, to the cent. A balance that does "
                    "not move in twelve months usually should have.")
        elif last_year == ZERO:
            status, note = "new", "Nil last year."
        elif key in EXPECTED_TO_MOVE:
            status, note = "normal", None
        else:
            # On magnitudes. A payables balance is held as a credit, so
            # signed arithmetic would report a liability growing from 40,000
            # to 89,220 as a fall of 123% - which reads as the opposite of
            # what happened.
            change = (abs(this_year) - abs(last_year)) / abs(last_year)
            if abs(change) >= LARGE_MOVEMENT:
                status = "moved"
                note = f"Changed by {change:+.0%}."
            else:
                status, note = "normal", None

        rows.append({
            "key": key, "label": label,
            "fs": entry["fs"] if entry else None,
            "this_year": this_year, "last_year": last_year,
            "difference": ((this_year or ZERO) - (last_year or ZERO)),
            "status": status, "note": note,
        })

    flagged = [r for r in rows if r["status"] != "normal"]
    return {
        "previous": previous,
        "rows": rows,
        "flagged": flagged,
        "unchanged": sum(1 for r in rows if r["status"] == "unchanged"),
        "moved": sum(1 for r in rows if r["status"] == "moved"),
        "new": sum(1 for r in rows if r["status"] == "new"),
        "gone": sum(1 for r in rows if r["status"] == "gone"),
    }


def check(financial_year):
    """Every outward check, or None when there is nothing yet to check."""
    evidence = evidence_checks(financial_year)
    movement = movements(financial_year)
    if not evidence and movement is None:
        return None

    return {
        "evidence": evidence,
        "movement": movement,
        "differs": sum(1 for f in evidence if f["status"] == "differs"),
        "missing": sum(1 for f in evidence if f["status"] == "missing"),
        "agrees": sum(1 for f in evidence if f["status"] == "agrees"),
    }


def prior_by_account(financial_year):
    """Last year's balance for each of this year's accounts.

    The firm's point 1: a trial balance exported from Xero or QuickBooks
    already carries a prior year column, and rebuilding the accounts without
    it throws away something the client has already paid for. The movement
    panel compares statement LINES, which is a different question - a
    preparer reading the grid wants the balance beside the account it belongs
    to, not aggregated with everything else that maps to the same line.

    Matched on (code, name) exactly as build() merges accounts, then on name
    alone. Name alone is the useful fallback, because a client renumbering
    their chart of accounts is common and renaming every account is not.
    Returns {this year's account id: last year's net balance}.
    """
    previous = previous_year(financial_year)
    if previous is None:
        return None

    last = (TrialBalanceAccount.query
            .filter_by(financial_year_id=previous.id).all())
    if not last:
        return None

    by_pair, by_name = {}, {}
    for account in last:
        code = (account.account_code or "").strip()
        name = (account.account_name or "").strip().lower()
        by_pair[(code, name)] = by_pair.get((code, name), ZERO) + _net(account)
        # A name held by two different codes cannot be matched on name
        # alone without guessing which one was meant, so it is withdrawn
        # rather than resolved arbitrarily.
        by_name[name] = (None if name in by_name and by_name[name] is None
                         else (None if name in by_name
                               else _net(account)))

    balances = {}
    for account in financial_year.tb_accounts:
        code = (account.account_code or "").strip()
        name = (account.account_name or "").strip().lower()
        if (code, name) in by_pair:
            balances[account.id] = by_pair[(code, name)]
        elif by_name.get(name) is not None:
            balances[account.id] = by_name[name]

    return {
        "year": previous,
        "balances": balances,
        # The net of what was matched. Nil means last year's accounts
        # balanced AND every one of them found a counterpart this year; a
        # figure means some of last year's accounts are not in this year's
        # grid, which is worth seeing rather than hiding.
        "total": sum(balances.values(), ZERO),
        "matched": len(balances),
        "of": len(last),
    }
