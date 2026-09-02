"""Where every figure in the report came from, and what never arrived.

Two questions an auditor has to be able to answer about a printed annual
report, and which no amount of careful arithmetic answers on its own:

    "This says revenue is 581,600 - made up of what?"
    "Is anything in the client's books missing from this report?"

The first is provenance. `app/services/statements.py` already records it:
each statement line stores `source_line_item_ids`, the trial balance accounts
that were summed into it. This module resolves those ids back into something
readable - account name and code, the amount each contributed, which uploaded
document it came from, and whether the mapping was Auditmate's guess or an
auditor's decision.

The second is coverage, and it had a real hole. The statement builder walks
the *template's* lines and pulls the accounts matching each one:

    for spec in template["lines"]:
        contributors = by_key.get(spec["key"], [])

An account whose `standard_key` is not a key any template uses is therefore
never looked at. It does not appear, and nothing reports it - the balance
sheet simply comes out short and nobody is told why. `unmapped` only counts
accounts with no standard_key at all, which is a different failure.

`coverage()` closes that: it compares what is in the trial balance against
what the templates actually consume, and names anything that falls between.
"""
import logging
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel

from ..models import (FinancialStatement, FinancialYear, TrialBalanceAccount)

log = logging.getLogger(__name__)

ZERO = Decimal("0")

# Statements whose lines are consumed from the standard trial balance. The
# trial balance statement itself mirrors the client's own accounts one for
# one, so it can never strand anything and is not part of the comparison.
ROLLUP_STATEMENTS = ("profit_and_loss", "balance_sheet",
                     "changes_in_equity", "cash_flow")


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------

def _contribution(account):
    """The signed amount this account carried into its statement line.

    The trial balance stores raw debits and credits; statements present a
    credit balance as a positive figure. The sign flip belongs to the mapping
    rule, so it is read from there rather than guessed from the sign.
    """
    from .mapping import match_label

    net = Decimal(str(account.debit or 0)) - Decimal(str(account.credit or 0))
    rule = match_label(account.account_name,
                       account.financial_year.customer_id)
    return net * (rule["sign"] if rule else 1)


def _account_detail(account):
    document = account.source_document
    return {
        "id": account.id,
        "code": account.account_code or "",
        "name": account.account_name,
        "debit": float(account.debit or 0),
        "credit": float(account.credit or 0),
        "amount": float(_contribution(account)),
        "standard_key": account.standard_key,
        "source": account.source,
        "document": document.original_filename if document else None,
        "category": (document.category_label
                     if document and hasattr(document, "category_label")
                     else (document.category if document else None)),
        # The single most useful fact on this panel: whether a human decided
        # this mapping or Auditmate guessed it.
        "mapped_by": "auditor" if account.mapping_is_manual else "auto",
        "confidence": account.confidence,
        "needs_review": account.needs_review,
        "is_adjustment": account.is_adjustment,
    }


def for_statement_line(line):
    """Everything behind one printed statement figure."""
    ids = line.source_line_item_ids or []
    accounts = []
    if ids:
        accounts = (TrialBalanceAccount.query
                    .filter(TrialBalanceAccount.id.in_(ids))
                    .order_by(TrialBalanceAccount.account_code,
                              TrialBalanceAccount.account_name)
                    .all())

    details = [_account_detail(a) for a in accounts]

    return {
        "line_id": line.id,
        "line_key": line.line_key,
        "label": line.effective_label,
        "label_overridden": line.label_is_overridden,
        "original_label": line.label,
        "amount": float(line.effective_amount or 0),
        "computed_amount": float(line.amount_current or 0),
        "overridden": line.is_overridden,
        # A computed line is the result of a formula over other lines, so it
        # has no accounts of its own - saying "no source" about it would be
        # wrong. Say what it actually is.
        "kind": ("computed" if line.is_computed
                 else "accounts" if details else "empty"),
        "formula": line.formula,
        "accounts": details,
        "total_from_accounts": float(sum((Decimal(str(d["amount"]))
                                          for d in details), ZERO)),
    }


def for_account(account_id):
    """Provenance for a single trial balance account, used by note tables."""
    account = TrialBalanceAccount.query.get(account_id)
    if account is None:
        return None
    return {"kind": "accounts", "accounts": [_account_detail(account)],
            "amount": float(_contribution(account))}


# --------------------------------------------------------------------------
# Coverage - what never made it into the report
# --------------------------------------------------------------------------

def _template_keys():
    """Every standard key the statement templates actually consume."""
    from .statements import template_for

    consumed = set()
    for statement_type in ROLLUP_STATEMENTS:
        for spec in (template_for(statement_type) or {}).get("lines", []):
            key = spec.get("key")
            if key:
                consumed.add(key)
    return consumed


# Documents describing last year. They never build this year's accounts and
# are not meant to - which is not the same as being unused.
PRIOR_YEAR_CATEGORIES = {"signed_accounts", "prior_trial_balance",
                         "prior_cash_flow"}


def _documents_not_used(financial_year):
    """Documents that were read but whose figures never reached the accounts.

    The firm asked for this directly: "if a value was extracted but not used,
    the generation screen should say why." Silence here is the worst answer -
    a document read successfully and then quietly ignored looks identical, on
    every screen, to one that was used.

    None of these are faults. Holding a document back is deliberate: exactly
    one document builds the accounts and the rest check them, or the money in
    them is counted twice. But deliberate is not the same as unexplained.
    """
    from .trial_balance import choose_sources

    documents = list(financial_year.documents)
    if not documents:
        return []

    sources, evidence = choose_sources(documents)
    source_names = ", ".join(sorted({d.category_label for d in sources})) \
        or "another document"
    evidence_ids = {d.id for d in evidence}

    out = []
    for document in documents:
        rows = len(document.line_items)
        if not rows:
            continue

        # Last year's documents are not evidence held back - they are used,
        # just somewhere else. Saying "not used" of a document that fills the
        # whole comparative column would be a false statement to an auditor,
        # and the wrong kind of false: one that invites them to go looking
        # for a problem that is not there.
        if document.category in PRIOR_YEAR_CATEGORIES:
            reason = ("Used for the comparative column, not for this year's "
                      "accounts — it describes last year.")
            if document.category == "signed_accounts":
                reason = ("Used for the comparative column and its note "
                          "wording, not for this year's accounts — it "
                          "describes last year.")
        elif document.review_status != "verified":
            reason = ("Not verified yet — verify it and the accounts rebuild "
                      "with its figures in.")
        elif document.id in evidence_ids:
            reason = (f"Held back on purpose to check the accounts against. "
                      f"{source_names} builds them; adding this one too would "
                      f"count the same money twice.")
        else:
            continue

        out.append({
            "id": document.id,
            "filename": document.original_filename,
            "category": document.category_label,
            "rows": rows,
            "reason": reason,
        })
    return out


def coverage(financial_year_id):
    """Reconcile the trial balance against what the report presents.

    Returns three groups, in descending order of how badly they need
    attention:

      unmapped  - no standard_key at all. Known about already; the trial
                  balance flags these.
      orphaned  - has a standard_key, but no statement template consumes it.
                  This is the silent one: the figure is mapped, looks fine on
                  the trial balance, and never reaches a statement.
      presented - accounted for.
    """
    financial_year = FinancialYear.query.get(financial_year_id)
    if financial_year is None:
        return {"ok": False, "error": "No such financial year."}

    accounts = (TrialBalanceAccount.query
                .filter_by(financial_year_id=financial_year_id)
                .order_by(TrialBalanceAccount.account_code,
                          TrialBalanceAccount.account_name)
                .all())

    consumed = _template_keys()

    unmapped, orphaned, presented = [], [], []
    for account in accounts:
        entry = {
            "id": account.id,
            "code": account.account_code or "",
            "name": account.account_name,
            "debit": float(account.debit or 0),
            "credit": float(account.credit or 0),
            "net": float(Decimal(str(account.debit or 0))
                         - Decimal(str(account.credit or 0))),
            "standard_key": account.standard_key,
            "mapped_by": "auditor" if account.mapping_is_manual else "auto",
        }
        if not account.standard_key:
            unmapped.append(entry)
        elif account.standard_key not in consumed:
            orphaned.append(entry)
        else:
            presented.append(entry)

    # Money at stake, not just a count. Two stranded accounts worth 12 dollars
    # and two worth 340,000 are not the same problem.
    def _weight(rows):
        return float(sum((abs(Decimal(str(r["net"]))) for r in rows), ZERO))

    missing = unmapped + orphaned
    not_used = _documents_not_used(financial_year)

    return {
        "ok": True,
        "not_used": not_used,
        "accounts_total": len(accounts),
        "presented": len(presented),
        "unmapped": unmapped,
        "orphaned": orphaned,
        "missing_count": len(missing),
        "missing_value": _weight(missing),
        "unmapped_value": _weight(unmapped),
        "orphaned_value": _weight(orphaned),
        "clean": not missing,
        "statements_built": FinancialStatement.query.filter_by(
            financial_year_id=financial_year_id).count(),
    }


def suggest(financial_year_id, use_ai=True):
    """Propose a standard key for every account the report is missing.

    Deterministic first: the same mapping rules that ran during the build get
    another go, because a rule may have been corrected since. Only what those
    still cannot place is sent to the AI, and only the account *names* go -
    never the figures, never the client's name.
    """
    from .mapping import match_label

    report = coverage(financial_year_id)
    if not report.get("ok"):
        return report

    financial_year = FinancialYear.query.get(financial_year_id)
    consumed = _template_keys()
    missing = report["unmapped"] + report["orphaned"]

    suggestions, still_stuck = [], []

    for entry in missing:
        rule = match_label(entry["name"], financial_year.customer_id)
        key = rule.get("line_key") if rule else None
        if key and key in consumed:
            suggestions.append({**entry, "suggested_key": key, "by": "rule",
                                "statement_type": rule.get("statement_type"),
                                "why": f"matches rule '{rule.get('pattern')}'",
                                "confidence": 0.85})
        else:
            still_stuck.append(entry)

    ai_error = None
    if use_ai and still_stuck:
        ai = _ai_suggest(still_stuck, sorted(consumed))
        suggestions.extend(ai["suggestions"])
        still_stuck = ai["unresolved"]
        ai_error = ai.get("error")

    return {"ok": True, "suggestions": suggestions,
            "unresolved": still_stuck,
            "ai_error": ai_error,
            "coverage": report}


class _Mapping(BaseModel):
    name: str
    key: Optional[str] = None
    confidence: float = 0.5
    why: str = ""


class _Mappings(BaseModel):
    mappings: List[_Mapping] = []


def _ai_suggest(accounts, allowed_keys):
    """Ask the configured AI to place account names on statement lines.

    Only the account *names* are sent - no figures, no client name, no
    document. A ledger account name on its own is not client financial data,
    which is what makes this safe to send at all, and the reason the amounts
    stay on this side of the call.
    """
    unresolved = list(accounts)

    try:
        from .extraction.providers import get_provider
        provider = get_provider()
        if not provider.available():
            return {"suggestions": [], "unresolved": unresolved,
                    "error": "No AI key configured."}
    except Exception:                                     # noqa: BLE001
        log.exception("AI provider unavailable")
        return {"suggestions": [], "unresolved": unresolved,
                "error": "AI provider unavailable."}

    system = (
        "You map accounting ledger account names onto the lines of a "
        "Singapore FRS/SFRS set of financial statements. "
        "Choose the single best key from the allowed list for each name. "
        "If none genuinely fits, return null for that name - a wrong mapping "
        "is worse than no mapping, because it silently misstates a statement "
        "while looking resolved."
    )
    text = "\n".join(
        ["Allowed keys:", ", ".join(allowed_keys), "", "Account names:"]
        + [f"- {a['name']}" for a in accounts])

    try:
        parsed = provider.structured_call(
            system, [{"type": "text", "text": text}], _Mappings,
            max_tokens=4000)
    except Exception:                                     # noqa: BLE001
        log.exception("AI suggestion failed")
        return {"suggestions": [], "unresolved": unresolved,
                "error": "The AI could not be reached."}

    by_name = {a["name"]: a for a in accounts}
    suggestions, placed = [], set()

    for item in parsed.mappings:
        entry = by_name.get(item.name)
        # Never accept a key the templates do not have. A plausible invention
        # strands the account just as surely, but stops anyone looking.
        if not entry or not item.key or item.key not in allowed_keys:
            continue
        suggestions.append({**entry, "suggested_key": item.key, "by": "ai",
                            "confidence": float(item.confidence or 0.5),
                            "why": item.why or ""})
        placed.add(item.name)

    return {"suggestions": suggestions,
            "unresolved": [a for a in accounts if a["name"] not in placed]}
