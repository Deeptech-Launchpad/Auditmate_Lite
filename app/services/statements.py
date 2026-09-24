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
        rule = match_label(account.account_name, financial_year.customer_id,
                           account_type=account.account_type)
        sign = rule["sign"] if rule else 1
        if sign == 1:
            # Either no name rule matched, or the rule is one learned from
            # a hand mapping - which is stored with sign=1 whatever the
            # account is (see classify.is_credit_balance). Both left every
            # credit balance such as "Amount Due from Director" or a bank
            # loan printing as a negative liability, which unbalanced the
            # balance sheet. The line the account was mapped to says which
            # side it lives on, so that decides. A rule that says -1 is
            # left alone: that is a contra account, deliberately flipped.
            from .classify import is_credit_balance
            if is_credit_balance(account.standard_key):
                sign = -1
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

    if not prior and not financial_year.is_first_year:
        # No previous engagement in Auditmate - which is every client's first
        # year here, so it is the normal case rather than the exception. The
        # comparative column is required DATA, not a check: without it the
        # statements cannot be issued at all. So fall back to last year's
        # signed accounts, or to Xero at last year's year end.
        from .prior_year import balances as _prior_balances

        figures, source = _prior_balances(financial_year)
        if figures:
            # balances() returns every source debit-positive, by design -
            # see prior_year._entered()'s own docstring, "TYPED AS
            # PRINTED, STORED AS PRINTED, RETURNED DEBIT-POSITIVE". A
            # preparer who reads 1,642,000 off last year's signed income
            # statement and types it gets it back from that function as
            # -1,642,000. Every other consumer of `prior` in this
            # function is stated as printed - a credit-side line like
            # revenue is positive - so it is flipped back here, once,
            # the same way present() flips a line code on its way out of
            # the note-level binding engine, and nowhere else: the OTHER
            # branch above (an earlier engagement's own effective_amount)
            # is already in this convention and must not be touched.
            from .classify import is_credit_balance

            prior = {key: (-value if is_credit_balance(key) else value)
                    for key, value in figures.items()}
            log.info("FY %s comparatives taken from %s",
                     financial_year.id, source)

            # A finished set of accounts prints only the lines that have a
            # balance. Where its balance sheet foots - assets equal equity
            # plus liabilities - a line it does not print was nil, and
            # showing "Incomplete" for it says the figure is unknown when the
            # statement has already settled it. Only the balance sheet can
            # be proved this way; a profit and loss that omits a detail
            # line has folded it into another, which is not the same as nil.
            if statement_type == "balance_sheet" and source in (
                    "signed_accounts", "entered"):
                from .classify import _index
                groups = {k: (v.get("group"))
                          for k, v in _index().items()}
                assets = sum((v for k, v in prior.items()
                              if groups.get(k) in ("current_assets",
                                                   "non_current_assets")),
                             ZERO)
                funded = sum((v for k, v in prior.items()
                              if groups.get(k) in (
                                  "equity", "current_liabilities",
                                  "non_current_liabilities")), ZERO)
                if assets and abs(assets - funded) <= Decimal("1"):
                    for spec in template.get("lines", []):
                        key = spec["key"]
                        if (not spec.get("formula") and key not in prior
                                and groups.get(key) in (
                                    "current_assets", "non_current_assets",
                                    "equity", "current_liabilities",
                                    "non_current_liabilities")):
                            prior[key] = ZERO

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
    # The comparative column gets the same treatment. Its figures arrive line
    # by line from last year's source, which supplies no subtotals - so
    # without this every total in the prior column stays blank.
    compute.apply_formulas_previous(
        lines, _prior_context(financial_year, statement_type))

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

        context["closing_share_capital"] = closing_share

        if financial_year.is_first_year:
            # A first period since incorporation opens at nil, and the
            # fallback below would be exactly wrong here. Carrying the
            # closing figures back as openings would show no share issue and
            # no movement in retained earnings - when in a first year the
            # shares WERE issued and the profit WAS earned inside the
            # period, and both belong in the statement of changes in equity
            # as movements. Nil is not a missing comparative here; it is the
            # fact.
            context["opening_share_capital"] = ZERO
            context["opening_retained_earnings"] = ZERO
        else:
            # With no comparative year on file, the trial balance's own
            # figures ARE the opening balances - they were brought forward.
            # Falling back to zero would invent a share issue and a
            # retained-earnings movement that never happened, and the
            # statement would not reconcile.
            context["opening_share_capital"] = prior_share or closing_share
            context["opening_retained_earnings"] = prior_accum or base_value(
                "balance_sheet", "retained_earnings")

    if statement_type == "cash_flow":
        # Opening and closing cash are facts, not derivations: closing cash
        # IS the balance sheet figure. Deriving it from movements and hoping
        # it agrees would let an unexplained gap pass unnoticed.
        #
        # prior_value only ever looks at an earlier engagement BUILT IN
        # AUDITMATE, which a first-year client has none of by definition -
        # every one of them, not an edge case. The balance sheet's own
        # comparative column already has an answer for exactly this
        # situation (see build_statement's own `prior` dict), and this
        # falls back to the same source it does: last year's signed
        # accounts, or its trial balance comparative, read through
        # prior_year.balances() directly, since that dict lives in the
        # caller and is not in scope here.
        #
        # cash_and_equivalents needs no sign correction the way a
        # credit-balance key would - it is never one - so the figure
        # balances() returns is already the presentation figure.
        # Without this, a first-year cash flow's opening balance was
        # read as nil and the entire year's movement fell through to
        # "Movement not yet analysed" - not the missing opening figure
        # alone, everything downstream of it too.
        opening_cash = prior_value("balance_sheet", "cash_and_equivalents")
        if not opening_cash and financial_year:
            from .prior_year import balances as _prior_balances

            fallback_figures, _source = _prior_balances(financial_year)
            opening_cash = (fallback_figures or {}).get(
                "cash_and_equivalents", ZERO)
        context["opening_cash"] = opening_cash
        context["closing_cash"] = statement_value("balance_sheet",
                                                  "cash_and_equivalents")
        # Last year's balance for a line, for working out movements.
        #
        # prior_value only reads an earlier engagement BUILT in Auditmate,
        # which most clients do not have. With none, every prior balance read
        # as nil and the whole closing balance was reported as this year's
        # movement - receivables, payables and tax alike - leaving hundreds
        # of thousands in "Movement not yet analysed". So where there is no
        # earlier engagement the same fallback the opening cash uses applies:
        # last year's signed accounts or trial-balance comparative.
        has_earlier = bool(
            financial_year and financial_year.previous_year_id
            and FinancialStatement.query.filter_by(
                financial_year_id=financial_year.previous_year_id,
                statement_type="balance_sheet").first())
        earlier_figures = {}
        if not has_earlier and financial_year:
            from .prior_year import balances as _prior_balances
            from .classify import is_credit_balance
            raw, _src = _prior_balances(financial_year)
            # balances() is debit-positive; statements print credit lines
            # positive, as in build_statement's own comparatives.
            earlier_figures = {
                key: (-value if is_credit_balance(key) else value)
                for key, value in (raw or {}).items()}

        def prior_balance(line_key):
            if has_earlier:
                return prior_value("balance_sheet", line_key)
            return Decimal(str(earlier_figures.get(line_key, 0) or 0))

        def moved(*keys):
            return sum((statement_value("balance_sheet", k) - prior_balance(k)
                        for k in keys), ZERO)

        # What tax cost, and how much of it is still owed. cf_tax_paid
        # reads the difference: the charge less the rise in the provision
        # is the cash that went.
        context["tax_expense"] = statement_value("profit_and_loss",
                                                 "tax_expense")
        context["tax_provision_movement"] = moved("tax_payable")
        # Working-capital movements: this year's balance less last year's.
        context["receivables_movement"] = moved(
            "trade_receivables", "prepayments", "inventories",
            "contract_assets")
        context["payables_movement"] = moved(
            "trade_payables", "accruals", "contract_liabilities")
        # Financing and investing, which had no formula and so never filled.
        context["borrowings_movement"] = moved(
            "short_term_borrowings", "long_term_borrowings")
        # Net book value of fixed assets rose by what was bought less what
        # was depreciated, so purchases = the rise plus the charge. A
        # disposal would need its own line; none is modelled here.
        context["ppe_net_movement"] = moved("ppe", "accumulated_depreciation")

    return context


def _prior_context(financial_year, statement_type):
    """Last year's cross-statement figures, for its equity and cash flow.

    Those two statements are derived from the others, so last year's column
    needs last year's profit, its closing balances AND the balances it opened
    with - the last of which no earlier engagement in this app supplies, and
    the signed accounts do, in their second column. None unless all of that
    is known: an equity statement built on a guessed opening balance is
    worse than one marked incomplete.
    """
    if statement_type not in ("changes_in_equity", "cash_flow"):
        return None
    if financial_year is None or financial_year.is_first_year:
        return None

    from .classify import is_credit_balance
    from .prior_year import balances, year_before_balances

    figures, source = balances(financial_year)
    if source not in ("signed_accounts", "entered"):
        return None
    before = year_before_balances(financial_year)
    if not figures or not before:
        return None

    def present(raw):
        return {key: (-value if is_credit_balance(key) else value)
                for key, value in raw.items()}

    closing, opening = present(figures), present(before)

    def at(book, key):
        return Decimal(str(book.get(key, 0) or 0))

    def moved(*keys):
        return sum((at(closing, k) - at(opening, k) for k in keys), ZERO)

    profit = FinancialStatement.query.filter_by(
        financial_year_id=financial_year.id,
        statement_type="profit_and_loss").first()
    last_year = {line.line_key: line.amount_previous
                 for line in (profit.lines if profit else [])}
    if last_year.get("total_comprehensive_income") is None:
        return None

    def lately(key):
        return Decimal(str(last_year.get(key) or 0))

    return {
        "total_comprehensive_income": lately("total_comprehensive_income"),
        "profit_for_year": lately("profit_for_year"),
        "profit_before_tax": lately("profit_before_tax"),
        "depreciation": lately("depreciation"),
        "tax_expense": lately("tax_expense"),
        "closing_share_capital": (at(closing, "share_capital")
                                  + at(closing, "working_capital")),
        "opening_share_capital": (at(opening, "share_capital")
                                  + at(opening, "working_capital")),
        "opening_retained_earnings": at(opening, "retained_earnings"),
        "closing_cash": at(closing, "cash_and_equivalents"),
        "opening_cash": at(opening, "cash_and_equivalents"),
        "tax_provision_movement": moved("tax_payable"),
        "receivables_movement": moved("trade_receivables", "prepayments",
                                      "inventories", "contract_assets"),
        "payables_movement": moved("trade_payables", "accruals",
                                   "contract_liabilities"),
        "borrowings_movement": moved("short_term_borrowings",
                                     "long_term_borrowings"),
        "ppe_net_movement": moved("ppe", "accumulated_depreciation"),
    }


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
    compute.apply_formulas_previous(
        statement.lines,
        _prior_context(statement.financial_year, statement.statement_type))
    db.session.commit()
