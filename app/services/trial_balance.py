"""The Standard Trial Balance.

Every input the engagement has - uploaded documents today, accounting-software
pulls later, plus the auditor's own adjustments - normalises into one table.
That table is the single source of truth: the financial statements are built
from it, the customer reviews it, and nothing downstream is produced until it
has been approved.

Two design points worth stating, because they shape everything else:

1. **Mapping happens here, once.** Each account is resolved to a
   `standard_key` - Auditmate's canonical account - at the moment it enters
   the trial balance. Statements then simply group by that key. Previously
   each statement resolved labels independently, which meant an unmapped
   account had to be fixed separately for every statement it touched.

2. **Rebuilding never destroys human work.** Auditor adjustments, manual
   edits and hand-assigned mappings survive a rebuild; only rows that came
   straight from a source are replaced.
"""
import logging
from datetime import datetime
from decimal import Decimal

from ..extensions import db
from ..models import (TB_SOURCE_PAIRED, TB_SOURCE_PRECEDENCE,
                      ExtractedLineItem, FinancialYear, TrialBalanceAccount)
from .audit import record
from .classify import is_credit_balance
from .extraction.base import looks_like_total_label
from .mapping import match_label

log = logging.getLogger(__name__)

ZERO = Decimal("0.00")

# Rows the auditor owns. A rebuild leaves these alone.
PROTECTED_SOURCES = {"manual", "adjustment"}

# An extracted label is TEXT and can be any length; an account name is
# VARCHAR(255). A ledger detail sheet carries whole scope-of-work narrations
# as descriptions, so the gap is not theoretical - one of them aborted a
# build with a database error and no explanation.
NAME_LIMIT = 255


def _fit_name(name: str) -> str:
    """Shorten an account name to what the column can hold.

    Visibly, so a truncated name reads as truncated rather than as an odd
    account. Nothing is lost: the full text stays on the extracted row the
    account was built from, which is what provenance links back to.
    """
    if len(name) <= NAME_LIMIT:
        return name
    return name[:NAME_LIMIT - 1].rstrip() + "…"


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------

def _amount_pair(item, standard_key=None):
    """Reduce an extracted row to (debit, credit).

    A workbook trial balance has its own debit and credit columns and those
    are used as given. A printed Profit & Loss or Balance Sheet does not: it
    has one amount column and no sides, and revenue, payables and share
    capital are all printed as positive numbers. Treating every one of them
    as a debit put 3.5m of sales on the wrong side of a real engagement and
    left the trial balance out by millions. Which side an account belongs on
    follows from the statement line it maps to.
    """
    debit = Decimal(str(item.debit)) if item.debit is not None else ZERO
    credit = Decimal(str(item.credit)) if item.credit is not None else ZERO

    if debit == ZERO and credit == ZERO and item.amount is not None:
        amount = Decimal(str(item.amount))
        on_credit = is_credit_balance(standard_key)
        if amount < 0:                    # a bracketed figure means the other side
            amount, on_credit = -amount, not on_credit
        return (ZERO, amount) if on_credit else (amount, ZERO)

    return debit, credit


def choose_sources(documents):
    """Which verified documents build the accounts, and which only check them.

    Returns (sources, evidence). A client sends several documents describing
    the same year and they overlap, so exactly one kind builds: the best that
    was supplied. Everything else is evidence.
    """
    verified = [d for d in documents if d.review_status == "verified"]

    # A Xero pull is a trial balance by another name and outranks a file.
    pulled = [d for d in verified if d.file_type == "xero"]
    if pulled:
        return pulled, [d for d in verified if d not in pulled]

    present = {(d.category or "other") for d in verified}
    for category in TB_SOURCE_PRECEDENCE:
        if category not in present:
            continue
        # The balance sheet and the profit and loss are two halves of one
        # source; taking one without the other would build half a year.
        wanted = ({category} | (TB_SOURCE_PAIRED & present)
                  if category in TB_SOURCE_PAIRED else {category})
        sources = [d for d in verified if (d.category or "other") in wanted]
        return sources, [d for d in verified if d not in sources]

    return [], verified


def _source_rows(financial_year_id, sources):
    """Every extracted row from the chosen source documents.

    Only verified documents reach here - an unchecked figure must never enter
    the trial balance, for the same reason it must never reach a statement.
    """
    if not sources:
        return []

    rows = (db.session.query(ExtractedLineItem)
            .filter(ExtractedLineItem.document_id.in_([d.id for d in sources]))
            .filter(ExtractedLineItem.status != "discarded")
            .all())

    # A source document's own total row would double every figure.
    return [r for r in rows if not looks_like_total_label(r.label)]


def build(financial_year_id: int, user_id=None) -> dict:
    """(Re)build the standard trial balance from all current sources."""
    financial_year = db.session.get(FinancialYear, financial_year_id)
    if financial_year is None:
        return {"ok": False, "error": "financial year not found"}

    if financial_year.tb_is_approved:
        return {"ok": False,
                "error": "This trial balance is approved and locked. Reopen "
                         "it before rebuilding."}

    sources, evidence = choose_sources(financial_year.documents)
    if not sources:
        return {"ok": False,
                "error": "None of the verified documents can build a trial "
                         "balance. Upload and verify a trial balance, a "
                         "general ledger, or both a balance sheet and a "
                         "profit and loss - then build again."}

    # Remember mappings the AUDITOR assigned, so a rebuild doesn't undo them.
    #
    # Only theirs. Remembering automatic mappings too meant a wrong guess
    # survived every rebuild: correcting the rule that produced it changed
    # nothing, because the old answer was carried forward as if a person had
    # chosen it.
    remembered = {
        (a.account_code or "", (a.account_name or "").lower()): a.standard_key
        for a in financial_year.tb_accounts
        if a.standard_key and a.mapping_is_manual
        and a.source not in PROTECTED_SOURCES
    }

    # Replace source-derived rows only; auditor rows are preserved.
    (TrialBalanceAccount.query
     .filter_by(financial_year_id=financial_year_id)
     .filter(~TrialBalanceAccount.source.in_(PROTECTED_SOURCES))
     .delete(synchronize_session=False))
    db.session.flush()

    customer_id = financial_year.customer_id
    merged = {}

    # Which channel each document arrived by, so the trial balance can say
    # "Xero" rather than "Uploaded document" for a pulled figure.
    channels = {d.id: ("xero" if d.file_type == "xero" else "upload")
                for d in financial_year.documents}

    for item in _source_rows(financial_year_id, sources):
        name = (item.label or "").strip() or "(unnamed account)"
        code = (item.account_code or "").strip()

        # Merge on the full label, not the shortened one. Two different
        # descriptions can share their first 255 characters, and adding
        # their figures together because of that would be a wrong number,
        # not a cosmetic problem.
        key = (code, name.lower())
        name = _fit_name(name)

        # The mapping is resolved before the amount, because a figure from a
        # printed statement has no side of its own and the line it maps to is
        # what decides one.
        standard_key = remembered.get(key)
        statement_type = None
        if not standard_key:
            rule = match_label(name, customer_id)
            if rule:
                standard_key = rule["line_key"]
                statement_type = rule["statement_type"]
                # A rule's sign flips credit-balance accounts for
                # presentation. The trial balance stores the raw debit and
                # credit, so the sign is applied later, when statements are
                # built - never here.

        debit, credit = _amount_pair(item, standard_key)

        if key in merged:
            # Same account arriving from two documents: add, don't duplicate.
            existing = merged[key]
            existing.debit = (existing.debit or ZERO) + debit
            existing.credit = (existing.credit or ZERO) + credit
            existing.confidence = min(existing.confidence or 1.0,
                                      item.confidence or 1.0)
            continue

        account = TrialBalanceAccount(
            financial_year_id=financial_year_id,
            account_code=code or None,
            account_name=name,
            standard_key=standard_key,
            statement_type=statement_type,
            debit=debit,
            credit=credit,
            source=channels.get(item.document_id, "upload"),
            source_document_id=item.document_id,
            source_ref=item.source_ref,
            confidence=item.confidence or 1.0,
            needs_review=not standard_key,
            mapping_is_manual=key in remembered,
            created_by=user_id,
        )
        db.session.add(account)
        merged[key] = account

    # Backfill statement_type for anything mapped but missing it.
    for account in merged.values():
        if account.standard_key and not account.statement_type:
            rule = match_label(account.account_name, customer_id)
            if rule:
                account.statement_type = rule["statement_type"]

    db.session.commit()

    totals = financial_year.tb_totals
    record("trial_balance", financial_year_id, "build",
           after={"accounts": totals["accounts"],
                  "unmapped": totals["unmapped"],
                  "balanced": totals["balanced"]}, commit=True)

    log.info("Built TB for FY %s: %s accounts, %s unmapped, balanced=%s",
             financial_year_id, totals["accounts"], totals["unmapped"],
             totals["balanced"])

    return {"ok": True, **totals,
            "built_from": [d.original_filename for d in sources],
            "checked_against": [d.original_filename for d in evidence]}


# --------------------------------------------------------------------------
# Editing
# --------------------------------------------------------------------------

def add_account(financial_year_id, account_name, debit=None, credit=None,
                account_code=None, standard_key=None, is_adjustment=False,
                notes=None, user_id=None):
    """Add a line the sources didn't provide, or an audit adjustment."""
    financial_year = db.session.get(FinancialYear, financial_year_id)
    if financial_year is None:
        return None

    rule = None
    if not standard_key:
        rule = match_label(account_name, financial_year.customer_id)

    account = TrialBalanceAccount(
        financial_year_id=financial_year_id,
        account_code=(account_code or "").strip() or None,
        account_name=_fit_name(account_name.strip()),
        standard_key=standard_key or (rule["line_key"] if rule else None),
        statement_type=rule["statement_type"] if rule else None,
        debit=Decimal(str(debit)) if debit not in (None, "") else ZERO,
        credit=Decimal(str(credit)) if credit not in (None, "") else ZERO,
        source="adjustment" if is_adjustment else "manual",
        is_adjustment=is_adjustment,
        confidence=1.0,
        needs_review=False,
        notes=notes,
        created_by=user_id,
    )
    db.session.add(account)
    record("trial_balance_account", None,
           "adjustment" if is_adjustment else "manual_add",
           after={"account": account.account_name,
                  "debit": str(account.debit), "credit": str(account.credit)})
    db.session.commit()
    return account


def set_mapping(account_id, standard_key, user_id=None, learn=True):
    """Assign an account to a statement line, and remember it for next year."""
    from .mapping import learn_mapping
    from .statements import line_keys_for, load_templates

    account = db.session.get(TrialBalanceAccount, account_id)
    if account is None:
        return {"ok": False, "error": "account not found"}

    statement_type = None
    for name in load_templates():
        if standard_key in line_keys_for(name):
            statement_type = name
            break

    if standard_key and statement_type is None:
        return {"ok": False, "error": f"unknown statement line {standard_key!r}"}

    before = account.standard_key
    account.standard_key = standard_key or None
    account.statement_type = statement_type
    account.needs_review = not standard_key
    # Chosen by a person, so a rebuild must not overwrite it.
    account.mapping_is_manual = bool(standard_key)

    # Remember the decision against this customer, so the same account maps
    # itself next year and on the next rebuild.
    if learn and standard_key:
        learn_mapping(account.financial_year.customer_id, account.account_name,
                      statement_type, standard_key, user_id)

    record("trial_balance_account", account.id, "map",
           before={"standard_key": before},
           after={"standard_key": standard_key})
    db.session.commit()
    return {"ok": True, "statement_type": statement_type}


def update_amounts(account_id, debit=None, credit=None, user_id=None):
    """Correct a figure on the trial balance."""
    account = db.session.get(TrialBalanceAccount, account_id)
    if account is None:
        return {"ok": False, "error": "account not found"}

    before = {"debit": str(account.debit), "credit": str(account.credit)}

    if debit is not None:
        account.debit = Decimal(str(debit)) if debit != "" else ZERO
    if credit is not None:
        account.credit = Decimal(str(credit)) if credit != "" else ZERO

    account.needs_review = False
    record("trial_balance_account", account.id, "edit", before=before,
           after={"debit": str(account.debit), "credit": str(account.credit)})
    db.session.commit()
    return {"ok": True}


def delete_account(account_id, user_id=None):
    account = db.session.get(TrialBalanceAccount, account_id)
    if account is None:
        return {"ok": False, "error": "account not found"}
    record("trial_balance_account", account.id, "delete",
           before={"account": account.account_name})
    db.session.delete(account)
    db.session.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# Approval - the gate everything downstream waits on
# --------------------------------------------------------------------------

def approve(financial_year_id, approved_by=None, user_id=None,
            force_unbalanced=False) -> dict:
    """Approve the trial balance, then build the statements from it.

    This is the gate in the new flow: statements and the audit report are
    produced only from an approved trial balance.
    """
    financial_year = db.session.get(FinancialYear, financial_year_id)
    if financial_year is None:
        return {"ok": False, "error": "financial year not found"}

    totals = financial_year.tb_totals

    if not totals["accounts"]:
        return {"ok": False, "error": "The trial balance is empty."}

    if totals["unmapped"]:
        return {"ok": False,
                "error": f"{totals['unmapped']} account(s) are not mapped to a "
                         f"statement line. Map them before approving - an "
                         f"unmapped account contributes to no statement."}

    if not totals["balanced"] and not force_unbalanced:
        return {"ok": False, "unbalanced": True,
                "error": f"Debits and credits differ by "
                         f"{totals['difference']:,.2f}. Fix the trial balance, "
                         f"or confirm you want to approve it anyway."}

    financial_year.tb_status = "approved"
    financial_year.tb_approved_at = datetime.utcnow()
    financial_year.tb_approved_by_name = approved_by

    record("trial_balance", financial_year_id, "approve",
           after={"by": approved_by, "balanced": totals["balanced"]})
    db.session.commit()

    # Statements follow automatically from the agreed trial balance.
    from .statements import build_all
    results = build_all(financial_year_id)

    return {"ok": True, "statements": results, **totals}


def reopen(financial_year_id, user_id=None) -> dict:
    """Unlock an approved trial balance so it can be corrected."""
    financial_year = db.session.get(FinancialYear, financial_year_id)
    if financial_year is None:
        return {"ok": False, "error": "financial year not found"}

    financial_year.tb_status = "draft"
    financial_year.tb_approved_at = None
    financial_year.tb_approved_by_name = None

    record("trial_balance", financial_year_id, "reopen", commit=True)
    return {"ok": True}


# --------------------------------------------------------------------------
# Queries used by the statement builder
# --------------------------------------------------------------------------

def accounts_for(financial_year_id, standard_key):
    """Trial balance accounts rolling up to one statement line."""
    return (TrialBalanceAccount.query
            .filter_by(financial_year_id=financial_year_id,
                       standard_key=standard_key)
            .all())


def unmapped(financial_year_id):
    return (TrialBalanceAccount.query
            .filter_by(financial_year_id=financial_year_id)
            .filter((TrialBalanceAccount.standard_key.is_(None))
                    | (TrialBalanceAccount.standard_key == ""))
            .order_by(TrialBalanceAccount.account_name)
            .all())
