"""Checking the trial balance against the documents it was NOT built from.

A client sends several documents describing the same year. Exactly one of
them builds the accounts - see trial_balance.choose_sources - because adding
overlapping documents together counts the same money twice. But the ones held
back are not waste: they are the client's own statement of what the figures
should be, and comparing them against what we computed is the single most
useful check in the file.

It is also rule 4 made real. A total from the client is for checking against;
a total in the report is computed by us. Two figures that agree are evidence.
One figure used twice is not.

The check that matters most is the one that finds nothing in the trial
balance at all: a real engagement had a balance sheet stating cash in bank of
163,753.90 against a trial balance with no bank account whatsoever, and the
only visible symptom was an unexplained difference of 50,790.22.
"""
import logging
from decimal import Decimal

from sqlalchemy import or_

from ..models import ExtractedLineItem, TrialBalanceAccount
from .classify import classify
from .extraction.base import looks_like_total_label
from .mapping import match_label

log = logging.getLogger(__name__)

ZERO = Decimal("0.00")

# What counts as agreement. Statements are usually presented to the dollar
# while a trial balance carries cents, so insisting on an exact match would
# report rounding as a finding and bury the real ones.
TOLERANCE = Decimal("1.00")

# Documents whose lines can be compared with a trial balance line for line.
#
# These state BALANCES: one figure per account, as at the year end. A general
# ledger, a bank statement or an invoice states MOVEMENTS instead - what
# happened during the year - and the two differ by every opening balance, so
# the comparison is meaningless even when both documents are perfectly
# correct. On a real engagement the ledger produced sixteen differences and
# three phantom omissions, none of them findings, next to 2,308 lines that
# matched nothing at all because they were named after suppliers. Real
# findings do not survive that much noise.
#
# The others are still held back and still available as evidence; they are
# simply not held up against the accounts as though they said the same thing.
COMPARABLE_CATEGORIES = {"trial_balance", "balance_sheet", "profit_and_loss"}


def _net(account) -> Decimal:
    """An account's balance as one signed figure, debit positive."""
    return (account.debit or ZERO) - (account.credit or ZERO)


def _evidence_amount(item) -> Decimal:
    """One evidence row as a signed figure, on the same footing as _net."""
    if item.debit is not None or item.credit is not None:
        return Decimal(str(item.debit or 0)) - Decimal(str(item.credit or 0))
    if item.amount is not None:
        return Decimal(str(item.amount))
    return ZERO


def check_document(document, accounts, customer_id):
    """Compare one evidence document against the built accounts.

    Every row is matched to a statement line, and the client's figure for that
    line is compared with ours. Rows that map to nothing are reported too -
    an account the client shows and we have never heard of is exactly the
    kind of omission this is for.
    """
    # Two ways to find our side of a line. By statement line first, which is
    # what a client's wording should resolve to. Then by the account's own
    # name, because an account that has not been mapped yet still exists -
    # and reporting it as missing would send an auditor hunting for a figure
    # that is sitting in front of them.
    ours = {}
    unmapped_names = {}
    for account in accounts:
        name = (account.account_name or "").strip().lower()
        if account.standard_key:
            ours[account.standard_key] = (ours.get(account.standard_key, ZERO)
                                          + _net(account))
        elif name:
            unmapped_names[name] = unmapped_names.get(name, ZERO) + _net(account)

    rows = (ExtractedLineItem.query
            .filter_by(document_id=document.id)
            .filter(ExtractedLineItem.status != "discarded")
            # Compare like with like: the client's figure for THIS year. Their
            # prior-year column would otherwise be added to it and every line
            # would differ by last year's balance.
            .filter(or_(ExtractedLineItem.period.is_(None),
                        ExtractedLineItem.period != "previous"))
            .all())

    # Add the client's rows up per statement line before comparing anything.
    #
    # A printed statement gives one line per account, but a general ledger
    # gives thousands of transactions - and each of those maps to the same
    # line. Compared one at a time, a single payment of 4.00 to a supplier
    # was held against a Cost of Services balance of 1,431,726.71 and
    # reported as a difference of 1,431,722.71, over and over for 2,339 rows.
    # The comparison an auditor wants is the ledger's total for a line
    # against ours.
    by_key, by_name, unmatched = {}, {}, 0

    for item in rows:
        label = (item.label or "").strip()
        if not label or looks_like_total_label(label):
            continue

        theirs = _evidence_amount(item)
        if theirs == ZERO:
            continue                      # a heading, or a line with no figure

        rule = match_label(label, customer_id)
        key = rule["line_key"] if rule else None

        if key is not None:
            bucket = by_key.setdefault(key, {"total": ZERO, "labels": set()})
        elif label.lower() in unmapped_names:
            bucket = by_name.setdefault(label.lower(),
                                        {"total": ZERO, "labels": set()})
        else:
            unmatched += 1
            continue

        bucket["total"] += theirs
        bucket["labels"].add(label)

    findings = []

    def _shown(bucket, fallback):
        """What to call a line the client wrote several ways."""
        labels = bucket["labels"]
        if len(labels) == 1:
            return next(iter(labels))
        return f"{fallback} ({len(labels)} lines)"

    for key, bucket in by_key.items():
        theirs = bucket["total"]
        entry = classify(key)
        label = _shown(bucket, entry["label"] if entry else key)

        if key not in ours:
            findings.append({
                "label": label, "theirs": theirs, "ours": None,
                "difference": theirs, "status": "missing"})
            continue

        # A statement prints revenue and liabilities as positive numbers while
        # the trial balance holds them as credits, so compare magnitudes.
        difference = abs(ours[key]) - abs(theirs)
        findings.append({
            "label": label, "theirs": theirs, "ours": ours[key],
            "difference": difference,
            "status": "agrees" if abs(difference) <= TOLERANCE else "differs"})

    for name, bucket in by_name.items():
        theirs = bucket["total"]
        difference = abs(unmapped_names[name]) - abs(theirs)
        findings.append({
            "label": _shown(bucket, name), "theirs": theirs,
            "ours": unmapped_names[name], "difference": difference,
            "status": "unmapped_account"})

    return findings, unmatched


def check(financial_year):
    """Every evidence document checked against the trial balance.

    Returns None when there is nothing to say - no accounts yet, or every
    verified document was used to build them.
    """
    from .trial_balance import choose_sources

    accounts = (TrialBalanceAccount.query
                .filter_by(financial_year_id=financial_year.id).all())
    if not accounts:
        return None

    _sources, evidence = choose_sources(financial_year.documents)
    if not evidence:
        return None

    comparable = [d for d in evidence
                  if (d.category or "other") in COMPARABLE_CATEGORIES]
    held_back = [d for d in evidence if d not in comparable]

    documents = []
    for document in comparable:
        findings, unmatched = check_document(document, accounts,
                                             financial_year.customer_id)
        if not findings:
            continue
        documents.append({
            "document": document,
            "unmatched": unmatched,
            "findings": sorted(
                findings,
                key=lambda f: (f["status"] == "agrees",
                               -abs(f["difference"] or f["theirs"]))),
            "agrees": sum(1 for f in findings if f["status"] == "agrees"),
            "differs": sum(1 for f in findings if f["status"] == "differs"),
            "missing": sum(1 for f in findings if f["status"] == "missing"),
            "needs_mapping": sum(1 for f in findings
                                 if f["status"] == "unmapped_account"),
        })

    if not documents and not held_back:
        return None

    return {
        "documents": documents,
        "held_back": held_back,
        "agrees": sum(d["agrees"] for d in documents),
        "differs": sum(d["differs"] for d in documents),
        "missing": sum(d["missing"] for d in documents),
        "needs_mapping": sum(d["needs_mapping"] for d in documents),
        "unmatched": sum(d["unmatched"] for d in documents),
    }
