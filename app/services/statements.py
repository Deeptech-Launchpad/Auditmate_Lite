"""Builds financial statements from verified document data.

Only *verified* documents feed the statements — a document the auditor hasn't
signed off in Review & Correct is deliberately excluded, so an unchecked
misread number can never reach a financial statement.
"""
import functools
import logging
from decimal import Decimal

import yaml
from flask import current_app

from ..extensions import db
from ..models import (Document, ExtractedLineItem, FinancialStatement,
                      FinancialYear, StatementLine)
from . import compute

log = logging.getLogger(__name__)
from .mapping import map_line_items

ZERO = Decimal("0.00")

# Which document categories feed which statement.
CATEGORY_SOURCES = {
    "trial_balance": ["trial_balance", "general_ledger", "balance_sheet",
                      "profit_and_loss"],
    "profit_and_loss": ["trial_balance", "profit_and_loss", "general_ledger",
                        "salary_schedule", "vendor_invoice"],
    "balance_sheet": ["trial_balance", "balance_sheet", "general_ledger",
                      "fixed_asset_register", "bank_statement"],
    "changes_in_equity": ["trial_balance", "general_ledger"],
    "cash_flow": ["trial_balance", "bank_statement", "general_ledger"],
    "accounts_receivable": ["receivables", "customer_invoice"],
    "accounts_payable": ["payables", "vendor_invoice"],
}


@functools.lru_cache(maxsize=1)
def load_templates():
    path = current_app.config["CONFIG_DIR"] / "statement_templates.yaml"
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def template_for(statement_type: str) -> dict:
    return load_templates().get(statement_type, {})


def line_keys_for(statement_type: str) -> list:
    return [l["key"] for l in template_for(statement_type).get("lines", [])]


def _verified_items(financial_year_id: int, statement_type: str):
    """Extracted rows from verified documents relevant to this statement."""
    categories = CATEGORY_SOURCES.get(statement_type, [])
    query = (db.session.query(ExtractedLineItem)
             .join(Document, ExtractedLineItem.document_id == Document.id)
             .filter(Document.financial_year_id == financial_year_id)
             .filter(Document.review_status == "verified")
             .filter(ExtractedLineItem.status != "discarded"))
    if categories:
        query = query.filter(Document.category.in_(categories))

    # Drop the source document's own total rows. They are a useful check in
    # Review & Correct, but feeding them into the statements would count
    # every underlying figure a second time.
    from .extraction.base import looks_like_total_label
    return [item for item in query.all()
            if not looks_like_total_label(item.label)]


def _amount_of(item) -> Decimal:
    """Reduce a line item to a single signed amount.

    Debit/credit listings become a net figure; single-amount rows pass through.
    """
    if item.amount is not None:
        return Decimal(str(item.amount))
    debit = Decimal(str(item.debit)) if item.debit is not None else ZERO
    credit = Decimal(str(item.credit)) if item.credit is not None else ZERO
    if debit or credit:
        return debit - credit
    return ZERO


def build_statement(financial_year_id: int, statement_type: str,
                    use_ai: bool = True) -> dict:
    """(Re)build one statement. Returns a summary for the UI."""
    financial_year = db.session.get(FinancialYear, financial_year_id)
    if financial_year is None:
        return {"ok": False, "error": "financial year not found"}

    template = template_for(statement_type)
    if not template:
        return {"ok": False, "error": f"no template for {statement_type}"}

    statement = FinancialStatement.query.filter_by(
        financial_year_id=financial_year_id,
        statement_type=statement_type).first()

    if statement is None:
        statement = FinancialStatement(
            financial_year_id=financial_year_id,
            statement_type=statement_type)
        db.session.add(statement)
        db.session.flush()

    # Preserve auditor overrides across a rebuild — that's the whole point of
    # storing them separately from the calculated figure.
    overrides = {l.line_key: l.manual_override_amount
                 for l in statement.lines if l.manual_override_amount is not None}

    StatementLine.query.filter_by(statement_id=statement.id).delete()
    db.session.flush()

    items = _verified_items(financial_year_id, statement_type)

    # ---- Trial balance is special: it mirrors the client's own accounts ----
    if statement_type == "trial_balance":
        lines = _build_trial_balance(statement, items, overrides)
        db.session.commit()
        return {"ok": True, "lines": len(lines), "unmapped": 0,
                "statement_id": statement.id}

    # ---- Everything else rolls up from the standard trial balance ----------
    #
    # Accounts were mapped to a `standard_key` when they entered the trial
    # balance, so there is no label matching to do here - just grouping. The
    # sign flip that turns a credit balance into a positive presentation
    # figure is applied at this point, because the trial balance itself always
    # stores raw debits and credits.
    from .mapping import match_label
    from ..models import TrialBalanceAccount

    tb_accounts = (TrialBalanceAccount.query
                   .filter_by(financial_year_id=financial_year_id)
                   .filter(TrialBalanceAccount.standard_key.isnot(None))
                   .all())

    by_key = {}
    for account in tb_accounts:
        by_key.setdefault(account.standard_key, []).append(account)

    def _signed(account):
        net = Decimal(str(account.debit or 0)) - Decimal(str(account.credit or 0))
        rule = match_label(account.account_name, financial_year.customer_id)
        sign = rule["sign"] if rule else 1
        return net * sign

    # ---- Comparatives ------------------------------------------------------
    # The prior year's figure for the SAME line, read from the statements
    # already built for that year. Read once into a dict rather than queried
    # per line, and left empty when there is no prior year - a first-year
    # engagement shows "--" in the comparative column, which is correct.
    prior = {}
    if financial_year.previous_year_id:
        earlier = FinancialStatement.query.filter_by(
            financial_year_id=financial_year.previous_year_id,
            statement_type=statement_type).first()
        if earlier:
            prior = {line.line_key: line.effective_amount
                     for line in earlier.lines}

    if not prior:
        # No previous engagement in Auditmate - which is every client's first
        # year here, so it is the normal case rather than the exception. The
        # comparative column is required DATA, not a check: without it the
        # statements cannot be issued at all. So fall back to last year's
        # signed accounts, or to Xero at last year's year end.
        from .prior_year import balances as _prior_balances

        figures, source = _prior_balances(financial_year)
        if figures:
            prior = figures
            log.info("FY %s comparatives taken from %s",
                     financial_year.id, source)

    lines = []
    for order, spec in enumerate(template.get("lines", [])):
        key = spec["key"]
        contributors = by_key.get(key, [])
        amount = sum((_signed(a) for a in contributors), ZERO)

        line = StatementLine(
            statement_id=statement.id,
            line_key=key,
            label=spec.get("label", key),
            group_key=spec.get("group"),
            sort_order=order,
            indent=spec.get("indent", 0),
            amount_current=amount,
            amount_previous=prior.get(key),
            base_amount=amount,
            is_subtotal=bool(spec.get("subtotal")),
            is_total=bool(spec.get("total")),
            is_detail=bool(spec.get("detail")),
            note_ref=str(spec["note"]) if spec.get("note") else None,
            is_computed=bool(spec.get("formula")),
            formula=spec.get("formula"),
            source="computed" if spec.get("formula") else "auto",
            # Provenance: which trial balance accounts make up this figure.
            source_line_item_ids=[a.id for a in contributors],
            manual_override_amount=overrides.get(key),
        )
        db.session.add(line)
        lines.append(line)

    # ---- Cross-statement context ------------------------------------------
    context = _build_context(financial_year_id, statement_type)
    compute.apply_formulas(lines, context)

    db.session.commit()

    # Unmapped accounts are now resolved on the trial balance, not per
    # statement, so this count is reported from there.
    from .trial_balance import unmapped as tb_unmapped
    stranded = tb_unmapped(financial_year_id)

    return {
        "ok": True,
        "lines": len(lines),
        "unmapped": len(stranded),
        "unmapped_labels": sorted({a.account_name for a in stranded})[:50],
        "statement_id": statement.id,
    }


def _build_trial_balance(statement, items, overrides):
    """Render the standard trial balance as a statement.

    Reads the `TrialBalanceAccount` table rather than raw extracted rows, so
    what appears here is exactly what the auditor approved and the customer
    reviewed - including any adjustments and manual corrections.
    """
    from ..models import TrialBalanceAccount

    accounts = (TrialBalanceAccount.query
                .filter_by(financial_year_id=statement.financial_year_id)
                .order_by(TrialBalanceAccount.account_code,
                          TrialBalanceAccount.account_name)
                .all())

    lines = []
    for order, account in enumerate(accounts):
        debit = Decimal(str(account.debit or 0))
        credit = Decimal(str(account.credit or 0))

        label = account.account_name
        if account.account_code:
            label = f"{account.account_code}  {label}"
        if account.is_adjustment:
            label = f"{label}  (adjustment)"

        line = StatementLine(
            statement_id=statement.id,
            line_key=f"tb_{account.id}",
            label=label,
            group_key="accounts",
            sort_order=order,
            amount_current=debit - credit,
            source="auto",
        )
        db.session.add(line)
        lines.append(line)

    total_debit = sum((l.amount_current for l in lines if l.amount_current > 0), ZERO)
    total_credit = sum((-l.amount_current for l in lines if l.amount_current < 0), ZERO)

    total = StatementLine(
        statement_id=statement.id,
        line_key="tb_total",
        label="Total",
        group_key="accounts",
        sort_order=len(lines),
        amount_current=total_debit - total_credit,
        is_total=True,
        source="computed",
    )
    db.session.add(total)
    lines.append(total)
    return lines


def _build_context(financial_year_id: int, statement_type: str) -> dict:
    """Gather figures this statement needs from other statements."""
    context = {}

    def statement_value(stype, line_key):
        statement = FinancialStatement.query.filter_by(
            financial_year_id=financial_year_id, statement_type=stype).first()
        if not statement:
            return ZERO
        for line in statement.lines:
            if line.line_key == line_key:
                return Decimal(str(line.effective_amount or 0))
        return ZERO

    financial_year = db.session.get(FinancialYear, financial_year_id)

    def prior_value(stype, line_key):
        """A figure from the SAME line in the previous financial year."""
        if not (financial_year and financial_year.previous_year_id):
            return ZERO
        previous = FinancialStatement.query.filter_by(
            financial_year_id=financial_year.previous_year_id,
            statement_type=stype).first()
        if not previous:
            return ZERO
        for line in previous.lines:
            if line.line_key == line_key:
                return Decimal(str(line.effective_amount or 0))
        return ZERO

    if statement_type in ("balance_sheet", "cash_flow", "changes_in_equity"):
        context["profit_for_year"] = statement_value("profit_and_loss",
                                                     "profit_for_year")
        context["profit_before_tax"] = statement_value("profit_and_loss",
                                                       "profit_before_tax")
        context["depreciation"] = statement_value("profit_and_loss", "depreciation")
        context["total_comprehensive_income"] = statement_value(
            "profit_and_loss", "total_comprehensive_income")

    if statement_type in ("changes_in_equity", "cash_flow"):
        def base_value(stype, line_key):
            """The figure straight from the trial balance, before formulas.

            Needed for opening balances. `retained_earnings` on the balance
            sheet is a computed line (opening + this year's profit), so
            reading its final value would double-count the profit.
            `base_amount` holds what the trial balance actually supplied.
            """
            statement = FinancialStatement.query.filter_by(
                financial_year_id=financial_year_id,
                statement_type=stype).first()
            if not statement:
                return ZERO
            for line in statement.lines:
                if line.line_key == line_key:
                    return Decimal(str(line.base_amount or 0))
            return ZERO

        # The client's template presents share capital and working capital as
        # a single equity column, so the two are combined here to make the
        # statement of changes in equity tie back to the balance sheet.
        closing_share = (statement_value("balance_sheet", "share_capital")
                         + statement_value("balance_sheet", "working_capital"))
        prior_share = (prior_value("balance_sheet", "share_capital")
                       + prior_value("balance_sheet", "working_capital"))
        prior_accum = prior_value("balance_sheet", "retained_earnings")

        # With no comparative year on file, the trial balance's own figures
        # ARE the opening balances - they were brought forward. Falling back
        # to zero would invent a share issue and a retained-earnings movement
        # that never happened, and the statement would not reconcile.
        context["closing_share_capital"] = closing_share
        context["opening_share_capital"] = prior_share or closing_share
        context["opening_retained_earnings"] = prior_accum or base_value(
            "balance_sheet", "retained_earnings")

    if statement_type == "cash_flow":
        # Opening and closing cash are facts, not derivations: closing cash
        # IS the balance sheet figure. Deriving it from movements and hoping
        # it agrees would let an unexplained gap pass unnoticed.
        context["opening_cash"] = prior_value("balance_sheet",
                                              "cash_and_equivalents")
        context["closing_cash"] = statement_value("balance_sheet",
                                                  "cash_and_equivalents")
        # Working-capital movements: this year's balance less last year's.
        context["receivables_movement"] = (
            statement_value("balance_sheet", "trade_receivables")
            + statement_value("balance_sheet", "prepayments")
            - prior_value("balance_sheet", "trade_receivables")
            - prior_value("balance_sheet", "prepayments"))
        context["payables_movement"] = (
            statement_value("balance_sheet", "trade_payables")
            - prior_value("balance_sheet", "trade_payables"))

    return context


def build_all(financial_year_id: int, use_ai: bool = True,
              cascade: bool = True) -> dict:
    """Rebuild every statement in the correct dependency order."""
    # Order matters: each statement feeds the next.
    order = ["trial_balance", "profit_and_loss", "balance_sheet",
             "changes_in_equity", "accounts_receivable",
             "accounts_payable", "cash_flow"]
    results = {}
    for statement_type in order:
        results[statement_type] = build_statement(
            financial_year_id, statement_type, use_ai=use_ai)

    if cascade:
        # A later year takes its comparative column from this one. Correcting
        # a prior year and leaving next year's comparatives showing the old
        # figures would be a wrong number in a signed report, so the
        # dependent year is rebuilt too. `cascade=False` stops it recursing.
        later = FinancialYear.query.filter_by(
            previous_year_id=financial_year_id).all()
        for year in later:
            if year.statements:
                log.info("Refreshing comparatives on FY %s after FY %s changed",
                         year.id, financial_year_id)
                build_all(year.id, use_ai=False, cascade=False)

    return results


def recalculate(statement_id: int) -> None:
    """Re-run formulas after an auditor edits a figure."""
    statement = db.session.get(FinancialStatement, statement_id)
    if statement is None:
        return
    context = _build_context(statement.financial_year_id,
                             statement.statement_type)
    compute.apply_formulas(statement.lines, context)
    db.session.commit()
