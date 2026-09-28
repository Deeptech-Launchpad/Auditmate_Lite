"""Sanity checks on the trial balance itself, before it is used for anything.

Library feedback A16: a real engagement's trial balance carried a Maybank
loan set up as a bank account ("MY-Term Laon"), a dividend posted straight
to retained earnings, and a director balance sitting in credit though its
name says "Amount Due FROM Director" - none of it caught by the one check
the engine already ran (debits equal credits), because all three balanced
perfectly.

Checks that need only the trial balance itself go here. A few of the
feedback's other examples - a balance unchanged all year, an account posted
to only once - need either a prior engagement to compare against (see
services/outward.py's movements(), which already flags "unchanged") or
ledger-level transaction detail a flat trial balance does not carry, so
they are not duplicated or guessed at here.

Advisory only, like every other check in this file's family (reconcile.py,
outward.py, depreciation_check.py): named for the preparer to look at, never
a gate. A false positive here would be a wrong exclusion rule shipped
without seeing real client data, which is a worse outcome than a finding
the preparer has to dismiss once.
"""
import re

from .classify import classify

# Loan-shaped words in an account name. A bank/cash account named for a loan
# almost always means the loan itself was set up as a bank account, rather
# than as a borrowing - the trial balance still balances either way, so
# nothing else catches it.
_LOAN_NAME = re.compile(r"\bloan\b", re.IGNORECASE)

# The standard lines a loan-named account should not be sitting under.
_CASH_KEYS = {"cash_and_equivalents"}

# "Due from X" names an amount the company is owed - an asset. "Due to X"
# names the reverse. Neither is a guess about what the account should be;
# it is what the client called it themselves.
_DUE_FROM = re.compile(r"\bdue from\b", re.IGNORECASE)
_DUE_TO = re.compile(r"\bdue to\b", re.IGNORECASE)

_ASSET_GROUPS = {"non_current_assets", "current_assets", "assets_total"}
_LIABILITY_GROUPS = {"non_current_liabilities", "current_liabilities",
                     "liabilities_total"}


def loan_named_accounts(financial_year):
    """Accounts whose name says "loan" but are mapped as cash or bank.

    Returns a list of {account, label} for the preparer to look at - the
    trial balance still balances with the loan sitting in the wrong place,
    which is exactly why nothing else in the app notices.
    """
    from ..models import TrialBalanceAccount

    found = []
    accounts = (TrialBalanceAccount.query
                .filter_by(financial_year_id=financial_year.id)
                .order_by(TrialBalanceAccount.account_name).all())
    for account in accounts:
        name = account.account_name or ""
        if not _LOAN_NAME.search(name):
            continue
        if account.standard_key not in _CASH_KEYS:
            continue
        entry = classify(account.standard_key)
        found.append({
            "account": account,
            "name": name,
            "mapped_as": entry["label"] if entry else account.standard_key,
        })
    return found


def direction_mismatches(financial_year):
    """Accounts whose own name says which way the money goes, mapped the
    other way (library feedback A16).

    "Amount Due from Director" is money the company is owed. Mapped to a
    payables line it becomes a liability, and the trial balance still
    foots - so nothing else notices, while the client's own balance sheet
    goes on reporting the same figure as a receivable. That is exactly the
    92,463 that the firm's own tested engagement could only find by
    comparing two of the client's documents by hand.

    Only the contradiction between an account's NAME and its MAPPING is
    reported. The balance's sign is deliberately not tested on its own: a
    contra-asset such as accumulated depreciation legitimately sits credit
    in an asset group, and a rule that did not know every such exception
    would flag a correct account on almost every engagement - which is how
    a checker teaches preparers to ignore it (feedback B3).
    """
    from ..models import TrialBalanceAccount

    found = []
    accounts = (TrialBalanceAccount.query
                .filter_by(financial_year_id=financial_year.id)
                .order_by(TrialBalanceAccount.account_name).all())
    for account in accounts:
        name = account.account_name or ""
        entry = classify(account.standard_key) or {}
        group = entry.get("group") or ""
        if not group:
            continue

        says, mapped_side = None, None
        if _DUE_FROM.search(name) and group in _LIABILITY_GROUPS:
            says, mapped_side = "owed to the company", "a liability"
        elif _DUE_TO.search(name) and group in _ASSET_GROUPS:
            says, mapped_side = "owed by the company", "an asset"
        if not says:
            continue

        found.append({
            "account": account,
            "name": name,
            "says": says,
            "mapped_side": mapped_side,
            "mapped_as": entry.get("label") or account.standard_key,
        })
    return found


def check(financial_year):
    """Every trial balance sanity check, keyed by what it found."""
    return {
        "loan_named": loan_named_accounts(financial_year),
        "direction": direction_mismatches(financial_year),
    }
