"""Figures for the notes to the financial statements.

The narrative in each note is auditor-editable text held in
config/report_sections.yaml. The *numbers* underneath must never be typed by
hand - they are derived here from the same approved trial balance the primary
statements come from, so a note can never disagree with the face of the
statement it explains.

A note declares what it needs with a `note_table:` block in
report_sections.yaml. Each block renders as one table:

    note_table:
      - source: accounts            # break the TB down account by account
        keys: [revenue]
        total: "Total revenue"      # omit for no total row

      - source: lines               # take generated statement lines
        statement: profit_and_loss
        keys: [cost_of_sales, operating_expenses]
        detail: true                # expand the group's breakdown lines
        total: true

      - source: tax_reconciliation  # the FRS 12 effective-rate reconciliation
      - source: currency            # cash split by denominating currency

Every figure carries a `ref` back to the trial balance account or statement
line it came from, so an auditor can trace it and the verification script can
prove the note ties to the statement it sits under.
"""
import logging
from decimal import Decimal

from ..models import FinancialStatement, TrialBalanceAccount

log = logging.getLogger(__name__)

# Singapore headline corporate tax rate. Named here rather than buried in a
# formula because it moves in Budget announcements.
CORPORATE_TAX_RATE = Decimal("0.17")


def _statements(financial_year):
    return {s.statement_type: s
            for s in FinancialStatement.query.filter_by(
                financial_year_id=financial_year.id).all()}


def _line(statements, statement_type, line_key):
    statement = statements.get(statement_type)
    if not statement:
        return None
    for line in statement.lines:
        if line.line_key == line_key:
            return line
    return None


def _accounts_for(financial_year, keys):
    """TB accounts rolling up to any of `keys`, largest balance first."""
    rows = (TrialBalanceAccount.query
            .filter(TrialBalanceAccount.financial_year_id == financial_year.id)
            .filter(TrialBalanceAccount.standard_key.in_(keys))
            .all())
    return sorted(rows, key=lambda a: abs(a.net or 0), reverse=True)


def _row(label, current, previous=None, *, bold=False, rule=False, ref=None):
    return {"label": label, "current": current, "previous": previous,
            "bold": bold, "rule": rule, "ref": ref}


# --------------------------------------------------------------------------
# Block builders
# --------------------------------------------------------------------------

def _block_accounts(spec, financial_year, statements):
    """One row per trial balance account inside the given standard keys.

    Income, equity and liability accounts are credit balances in the trial
    balance. The note presents them the way the statement does - revenue of
    581,600, not (581,600).
    """
    keys = spec.get("keys") or []
    rows = []

    for account in _accounts_for(financial_year, keys):
        amount = Decimal(str(account.net or 0))
        if amount < 0:
            amount = -amount
        rows.append(_row(account.account_name, amount, ref=f"tb:{account.id}"))

    if not rows:
        return None

    if "total" in spec and len(rows) > 1:
        label = spec["total"] if isinstance(spec["total"], str) else ""
        rows.append(_row(label, sum(r["current"] for r in rows),
                         bold=True, rule=True))

    return {"heading": spec.get("heading"), "rows": rows,
            "columns": spec.get("columns")}


def _block_lines(spec, financial_year, statements):
    """Rows taken from the generated statement lines."""
    statement_type = spec.get("statement", "profit_and_loss")
    statement = statements.get(statement_type)
    if not statement:
        return None

    keys = spec.get("keys") or []
    want_detail = bool(spec.get("detail"))
    rows = []

    for key in keys:
        head = _line(statements, statement_type, key)
        if head is None:
            continue

        if want_detail:
            # Expand the group's breakdown lines, skipping empty ones so a
            # note does not list twenty zero rows.
            for line in statement.lines:
                if (line.group_key == head.group_key and line.is_detail
                        and (line.effective_amount or 0) != 0):
                    rows.append(_row(line.label,
                                     Decimal(str(line.effective_amount or 0)),
                                     line.amount_previous,
                                     ref=f"line:{line.line_key}"))
        else:
            amount = Decimal(str(head.effective_amount or 0))
            # A nil line is noise in a note; the statement already shows it.
            if amount == 0 and not spec.get("keep_zero"):
                continue
            rows.append(_row(head.label, amount, head.amount_previous,
                             ref=f"line:{head.line_key}"))

    if not rows:
        return None

    if "total" in spec and len(rows) > 1:
        label = spec["total"] if isinstance(spec["total"], str) else ""
        rows.append(_row(
            label,
            sum(r["current"] for r in rows),
            # Only foot the comparative column when every row has one -
            # a partial total would be worse than no total.
            sum(Decimal(str(r["previous"])) for r in rows)
            if all(r["previous"] is not None for r in rows) else None,
            bold=True, rule=True))

    return {"heading": spec.get("heading"), "rows": rows,
            "columns": spec.get("columns")}


def _block_tax(spec, financial_year, statements):
    """FRS 12 reconciliation of tax expense to accounting profit.

    The balancing figure is presented as exemptions and allowances, which is
    what it almost always is for a Singapore SME claiming the partial tax
    exemption - and it makes the note tie to the charge by construction
    rather than by hope.
    """
    pbt_line = _line(statements, "profit_and_loss", "profit_before_tax")
    tax_line = _line(statements, "profit_and_loss", "tax_expense")
    if pbt_line is None or tax_line is None:
        return None

    pbt = Decimal(str(pbt_line.effective_amount or 0))
    charge = Decimal(str(tax_line.effective_amount or 0))
    at_rate = (pbt * CORPORATE_TAX_RATE).quantize(Decimal("0.01"))
    balancing = charge - at_rate

    rate_label = "Tax calculated at a tax rate of {:.0%}".format(
        CORPORATE_TAX_RATE)

    return {
        "heading": spec.get("heading",
                            "Relationship between tax expense and "
                            "accounting profit"),
        "rows": [
            _row("Profit before income tax", pbt, pbt_line.amount_previous),
            _row(rate_label, at_rate),
            _row("Effects of:", None),
            _row("Non-deductible expenses",
                 balancing if balancing > 0 else Decimal("0")),
            _row("Tax exemptions and allowances",
                 balancing if balancing < 0 else Decimal("0")),
            _row("Tax charge", charge, tax_line.amount_previous,
                 bold=True, rule=True),
        ],
        "columns": spec.get("columns"),
    }


def _block_currency(spec, financial_year, statements):
    """Cash split by denominating currency.

    Auditmate does not yet capture a currency per account, so the whole
    balance sits in the reporting currency. Shown explicitly rather than
    silently, so the auditor knows to override it if the client holds
    foreign currency.
    """
    line = _line(statements, "balance_sheet", "cash_and_equivalents")
    if line is None:
        return None

    currency = financial_year.customer.books_currency or "SGD"
    names = {"SGD": "Singapore Dollar", "USD": "US Dollar",
             "MYR": "Malaysian Ringgit", "EUR": "Euro",
             "GBP": "Pound Sterling"}

    return {
        "heading": spec.get(
            "heading",
            "Cash and cash equivalents are denominated in the following "
            "currencies:"),
        "rows": [_row(names.get(currency, currency),
                      line.effective_amount or 0, line.amount_previous,
                      bold=True)],
        "columns": spec.get("columns"),
    }


BUILDERS = {
    "accounts": _block_accounts,
    "lines": _block_lines,
    "tax_reconciliation": _block_tax,
    "currency": _block_currency,
}


def build_tables(spec, financial_year):
    """Turn a section's `note_table:` config into renderable tables."""
    if not spec:
        return []

    if isinstance(spec, dict):
        spec = [spec]

    statements = _statements(financial_year)
    tables = []

    for block in spec:
        builder = BUILDERS.get(block.get("source", "accounts"))
        if builder is None:
            log.warning("Unknown note_table source: %s", block.get("source"))
            continue
        try:
            table = builder(block, financial_year, statements)
        except Exception:                              # noqa: BLE001
            # A broken note must not take the whole report down; the auditor
            # sees the narrative and no table rather than a 500.
            log.exception("Note table failed: %s", block)
            continue
        if table and table.get("rows"):
            tables.append(table)

    return tables
