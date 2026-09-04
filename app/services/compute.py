"""Derived statement figures.

Every formula is a named Python function looked up in a registry — there is
deliberately no `eval()` anywhere near auditor-editable content.

Calculation order matters because statements feed each other:
    Trial Balance -> P&L -> Balance Sheet (retained earnings) -> Cash Flow
"""
from decimal import Decimal

ZERO = Decimal("0.00")


def _sum_group(lines, group_key, exclude_subtotals=True):
    total = ZERO
    for line in lines:
        if line.group_key != group_key:
            continue
        if exclude_subtotals and (line.is_subtotal or line.is_total):
            continue
        total += Decimal(str(line.effective_amount or 0))
    return total


def _value(lines, line_key):
    for line in lines:
        if line.line_key == line_key:
            return Decimal(str(line.effective_amount or 0))
    return ZERO


# --------------------------------------------------------------------------
# Formula implementations
#
# Each takes (lines, context) and returns a Decimal. `context` carries
# cross-statement values, e.g. the P&L result when computing the balance sheet.
# --------------------------------------------------------------------------

def sum_group_income(lines, ctx):
    return _sum_group(lines, "income")


def sum_group_expenses(lines, ctx):
    return _sum_group(lines, "expenses")


def sum_group_cost_of_sales(lines, ctx):
    return _sum_group(lines, "cost_of_sales")


def sum_group_operating_expenses(lines, ctx):
    return _sum_group(lines, "operating_expenses")


def gross_profit(lines, ctx):
    """Revenue less cost of sales - the client template's presentation."""
    return _value(lines, "revenue") - _value(lines, "cost_of_sales")


def profit_before_tax(lines, ctx):
    return _value(lines, "gross_profit") - _value(lines, "operating_expenses")


def profit_for_year(lines, ctx):
    return _value(lines, "profit_before_tax") - _value(lines, "tax_expense")


def total_comprehensive_income(lines, ctx):
    return (_value(lines, "profit_for_year")
            + _value(lines, "other_comprehensive_income"))


def sum_group_non_current_assets(lines, ctx):
    return _sum_group(lines, "non_current_assets")


def sum_group_current_assets(lines, ctx):
    return _sum_group(lines, "current_assets")


def total_assets(lines, ctx):
    return (_value(lines, "total_non_current_assets")
            + _value(lines, "total_current_assets"))


def _base(lines, line_key):
    """The pre-formula, straight-from-the-documents figure for a line."""
    for line in lines:
        if line.line_key == line_key:
            return Decimal(str(line.base_amount or 0))
    return ZERO


def retained_earnings(lines, ctx):
    """Opening retained earnings plus this year's profit.

    This is the link that makes the balance sheet depend on the P&L, which is
    why the build order is fixed.

    The opening balance is read from `base_amount` - the figure the trial
    balance supplied - and never from the line's current value. Reading the
    current value would make this non-idempotent: the second recalculation
    would treat last time's result as the opening balance and add the year's
    profit all over again.
    """
    opening = _base(lines, "retained_earnings")
    if not opening:
        opening = Decimal(str(ctx.get("opening_retained_earnings", 0)))
    profit = Decimal(str(ctx.get("profit_for_year", 0)))
    return opening + profit


def sum_group_equity(lines, ctx):
    return _sum_group(lines, "equity")


def sum_group_non_current_liabilities(lines, ctx):
    return _sum_group(lines, "non_current_liabilities")


def sum_group_current_liabilities(lines, ctx):
    return _sum_group(lines, "current_liabilities")


def total_equity_and_liabilities(lines, ctx):
    return (_value(lines, "total_equity")
            + _value(lines, "total_non_current_liabilities")
            + _value(lines, "total_current_liabilities"))


# --- Cash flow (indirect method) ------------------------------------------

def cf_profit_before_tax(lines, ctx):
    return Decimal(str(ctx.get("profit_before_tax", 0)))


def cf_depreciation(lines, ctx):
    return Decimal(str(ctx.get("depreciation", 0)))


def sum_group_operating(lines, ctx):
    return _sum_group(lines, "operating")


def sum_group_investing(lines, ctx):
    return _sum_group(lines, "investing")


def sum_group_financing(lines, ctx):
    return _sum_group(lines, "financing")


def cf_net_change(lines, ctx):
    """Movement in cash: closing less opening. A fact, not a derivation."""
    return (Decimal(str(ctx.get("closing_cash", 0)))
            - Decimal(str(ctx.get("opening_cash", 0))))


def cf_unexplained(lines, ctx):
    """The part of the cash movement the analysed sections do not account for.

    The three sections should add up to the actual movement in cash. Where
    they do not - most commonly a first year with no comparative balances, so
    no movements can be computed - the gap is shown explicitly rather than
    being buried or silently forced to zero. An auditor needs to see what has
    not been explained.
    """
    movement = (Decimal(str(ctx.get("closing_cash", 0)))
                - Decimal(str(ctx.get("opening_cash", 0))))
    analysed = (_value(lines, "cf_operating_total")
                + _value(lines, "cf_investing_total")
                + _value(lines, "cf_financing_total"))
    return movement - analysed


def cf_opening_cash(lines, ctx):
    return Decimal(str(ctx.get("opening_cash", 0)))


def cf_closing_cash(lines, ctx):
    """Cash at the year end - taken from the balance sheet, by definition."""
    return Decimal(str(ctx.get("closing_cash", 0)))


def sum_group_ageing(lines, ctx):
    return _sum_group(lines, "ageing")


# --- Statement of Changes in Equity ---------------------------------------
#
# Reconciles opening equity to closing equity. The template presents it as a
# matrix (Share Capital | Accumulated Profit | Total); these formulas fill the
# nine cells, and the preview lays them back out as a grid.
#
# Note on presentation: shares issued during the year are a transaction with
# owners, NOT comprehensive income, so they are kept on their own line rather
# than folded into the income row.

def soce_open_share(lines, ctx):
    return Decimal(str(ctx.get("opening_share_capital", 0)))


def soce_open_accum(lines, ctx):
    return Decimal(str(ctx.get("opening_retained_earnings", 0)))


def soce_open_total(lines, ctx):
    return _value(lines, "soce_open_share") + _value(lines, "soce_open_accum")


def soce_issue_share(lines, ctx):
    """Shares issued in the year = closing share capital less opening."""
    return (Decimal(str(ctx.get("closing_share_capital", 0)))
            - Decimal(str(ctx.get("opening_share_capital", 0))))


def soce_income_accum(lines, ctx):
    return Decimal(str(ctx.get("total_comprehensive_income", 0)))


def soce_move_total(lines, ctx):
    return (_value(lines, "soce_issue_share")
            + _value(lines, "soce_income_accum"))


def soce_close_share(lines, ctx):
    return _value(lines, "soce_open_share") + _value(lines, "soce_issue_share")


def soce_close_accum(lines, ctx):
    return _value(lines, "soce_open_accum") + _value(lines, "soce_income_accum")


def soce_close_total(lines, ctx):
    return _value(lines, "soce_close_share") + _value(lines, "soce_close_accum")


# --- Cash flow additions --------------------------------------------------

def cf_receivables(lines, ctx):
    """Movement in receivables. An increase consumes cash, hence negative."""
    return -(Decimal(str(ctx.get("receivables_movement", 0))))


def cf_payables(lines, ctx):
    """Movement in payables. An increase releases cash, hence positive."""
    return Decimal(str(ctx.get("payables_movement", 0)))


def cf_share_capital(lines, ctx):
    return (Decimal(str(ctx.get("closing_share_capital", 0)))
            - Decimal(str(ctx.get("opening_share_capital", 0))))


def cf_operating_total(lines, ctx):
    return (_value(lines, "cf_operations")
            + _value(lines, "cf_tax_paid")
            + _value(lines, "cf_expenses_paid"))


# --------------------------------------------------------------------------
# Registry — formulas are referenced by name from statement_templates.yaml
# --------------------------------------------------------------------------

FORMULAS = {
    "sum_group_income": sum_group_income,
    "sum_group_expenses": sum_group_expenses,
    "profit_before_tax": profit_before_tax,
    "profit_for_year": profit_for_year,
    "sum_group_non_current_assets": sum_group_non_current_assets,
    "sum_group_current_assets": sum_group_current_assets,
    "total_assets": total_assets,
    "retained_earnings": retained_earnings,
    "sum_group_equity": sum_group_equity,
    "sum_group_non_current_liabilities": sum_group_non_current_liabilities,
    "sum_group_current_liabilities": sum_group_current_liabilities,
    "total_equity_and_liabilities": total_equity_and_liabilities,
    "cf_profit_before_tax": cf_profit_before_tax,
    "cf_depreciation": cf_depreciation,
    "sum_group_operating": sum_group_operating,
    "sum_group_investing": sum_group_investing,
    "sum_group_financing": sum_group_financing,
    "cf_net_change": cf_net_change,
    "cf_unexplained": cf_unexplained,
    "cf_opening_cash": cf_opening_cash,
    "cf_closing_cash": cf_closing_cash,
    "sum_group_ageing": sum_group_ageing,
    "sum_group_cost_of_sales": sum_group_cost_of_sales,
    "sum_group_operating_expenses": sum_group_operating_expenses,
    "gross_profit": gross_profit,
    "total_comprehensive_income": total_comprehensive_income,
    "soce_open_share": soce_open_share,
    "soce_open_accum": soce_open_accum,
    "soce_open_total": soce_open_total,
    "soce_issue_share": soce_issue_share,
    "soce_income_accum": soce_income_accum,
    "soce_move_total": soce_move_total,
    "soce_close_share": soce_close_share,
    "soce_close_accum": soce_close_accum,
    "soce_close_total": soce_close_total,
    "cf_receivables": cf_receivables,
    "cf_payables": cf_payables,
    "cf_share_capital": cf_share_capital,
    "cf_operating_total": cf_operating_total,
}


def apply_formulas(lines, context=None):
    """Recalculate every computed line, in template order.

    Template order matters: subtotals are defined after the lines they sum,
    so a single forward pass resolves the dependencies correctly.
    """
    context = context or {}
    for line in lines:
        if not line.formula:
            continue
        func = FORMULAS.get(line.formula)
        if func is None:
            continue
        # An auditor override always wins — never overwrite a manual figure.
        if line.manual_override_amount is not None:
            continue
        try:
            line.amount_current = func(lines, context)
        except Exception:                          # noqa: BLE001
            line.amount_current = ZERO
    return lines


class _PriorLine:
    """A statement line seen through its prior-year column.

    Every formula reads `effective_amount`, so presenting last year's figure
    under that name lets the same formulas produce the comparative column,
    rather than a second implementation that would drift out of step with
    this one the first time a subtotal changed.
    """
    __slots__ = ("line_key", "group_key", "formula", "is_subtotal",
                 "is_total", "amount_current", "base_amount",
                 "manual_override_amount")

    def __init__(self, line):
        self.line_key = line.line_key
        self.group_key = line.group_key
        self.formula = line.formula
        self.is_subtotal = line.is_subtotal
        self.is_total = line.is_total
        self.amount_current = line.amount_previous
        self.base_amount = line.amount_previous
        self.manual_override_amount = None

    @property
    def effective_amount(self):
        return self.amount_current


class _ContextProbe(dict):
    """A context that records whether a formula reached into it.

    The context carries CROSS-STATEMENT figures - this year's profit, this
    year's closing cash. They are current-year values, so a formula that
    needs one cannot produce last year's comparative from them. Recording the
    reach lets the caller discard that line instead of printing this year's
    number in last year's column, which would look entirely plausible.
    """

    def __init__(self):
        super().__init__()
        self.touched = False

    def get(self, key, default=None):
        self.touched = True
        return default

    def __getitem__(self, key):
        self.touched = True
        raise KeyError(key)

    def __contains__(self, key):
        self.touched = True
        return False


def apply_formulas_previous(lines):
    """Compute the comparative column's subtotals and totals.

    Without this every computed line - gross profit, profit before tax, total
    assets, total comprehensive income - prints "--" in the prior column while
    the lines feeding it show figures, because the formulas only ever ran over
    the current column.

    Skipped entirely when there are no prior figures at all: a first-year
    engagement must print "--" down that column, and summing a column of
    nothing would state last year's profit as a confident 0.00.
    """
    if not any(line.amount_previous is not None for line in lines):
        return lines

    views = [_PriorLine(line) for line in lines]
    by_key = {view.line_key: view for view in views}

    for line in lines:
        if not line.formula:
            continue
        func = FORMULAS.get(line.formula)
        if func is None:
            continue

        probe = _ContextProbe()
        try:
            value = func(views, probe)
        except Exception:                              # noqa: BLE001
            continue
        if probe.touched:
            continue

        # Written back to the view too, so a later subtotal summing this one
        # reads the figure just computed rather than the None it started as.
        view = by_key.get(line.line_key)
        if view is not None:
            view.amount_current = value
        line.amount_previous = value

    return lines


def balance_check(lines) -> dict:
    """Does the balance sheet actually balance?"""
    assets = _value(lines, "total_assets")
    equity_liabilities = _value(lines, "total_equity_and_liabilities")
    difference = assets - equity_liabilities
    return {
        "assets": assets,
        "equity_and_liabilities": equity_liabilities,
        "difference": difference,
        "balanced": abs(difference) < Decimal("0.01"),
    }
