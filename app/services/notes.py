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
from . import prior_year
from .outward import previous_year as outward_previous_year
from .prior_year import ZERO

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

def _previous_totals_by_key(financial_year, keys):
    """Prior-year figures, summed per standard key.

    Matched by key rather than by account: a client's own account names
    are free text and are not the same row from one year to the next
    (a bank account renamed, two accounts merged), but the standard key
    an account was mapped to is stable, and it is the same thing every
    other comparative in this app is matched on.

    Drawn from the same waterfall of sources the statement FACE gets its
    comparative column from (see prior_year.balances) - a separate, narrower
    query here once meant a client whose only prior year was a trial
    balance's own comparative column, a signed accounts PDF, or a Xero pull
    (the code's own description of the normal case, not the exception) saw
    the comparative on every statement but "--" in every note.
    """
    figures, _source = prior_year.balances(financial_year)
    return {key: (amount if amount >= 0 else -amount)
            for key, amount in figures.items() if key in keys}


def _same_account(name) -> str:
    """A client account name reduced to what is comparable between years."""
    return " ".join((name or "").split()).strip().lower()


def _previous_by_account(financial_year, keys):
    """Last year per ACCOUNT, not merely per key - where that can be known.

    A key-level total is the right granularity for a statement line, which
    shows one figure per line, and it is what prior_year.balances returns.
    A note is different: it lists one row per named account, and several
    accounts routinely share one key (Sales and Service Income are both
    revenue). Handing each of those rows the same key-level total states
    every one of them at the combined figure, and a total row then adds
    that same figure once per account - last year's revenue reported at
    double, on the face of a note.

    Only the two sources that keep account-level detail can answer this:
    a previous engagement's own trial balance, and the comparative column
    this year's trial balance carries per account. A figure read out of a
    signed-accounts PDF has been through a statement and no longer has
    accounts to match. Returns {(key, comparable name): amount}, and the
    caller leaves a row blank rather than guessing when it is not in here.
    """
    matched = {}

    # This year's own accounts, each carrying its prior column. Nothing to
    # match by name at all: the figure is already on the row it belongs to.
    for account in _accounts_for(financial_year, keys):
        if account.prior_debit is None and account.prior_credit is None:
            continue
        net = (Decimal(str(account.prior_debit or 0))
               - Decimal(str(account.prior_credit or 0)))
        matched[(account.standard_key, _same_account(account.account_name))] = (
            net if net >= 0 else -net)

    # The previous engagement's approved trial balance, by account name.
    # Ranked after the above only because it is reached second; the two
    # rarely both exist, and where they do they are the same client's same
    # accounts.
    previous = outward_previous_year(financial_year)
    if previous is not None:
        prior_accounts = (TrialBalanceAccount.query
                          .filter(TrialBalanceAccount.financial_year_id
                                  == previous.id)
                          .filter(TrialBalanceAccount.standard_key.in_(keys))
                          .all())
        for account in prior_accounts:
            slot = (account.standard_key, _same_account(account.account_name))
            if slot in matched:
                continue
            net = Decimal(str(account.net or 0))
            matched[slot] = net if net >= 0 else -net

    return matched


def _block_accounts(spec, financial_year, statements):
    """One row per trial balance account inside the given standard keys.

    Income, equity and liability accounts are credit balances in the trial
    balance. The note presents them the way the statement does - revenue of
    581,600, not (581,600).
    """
    keys = spec.get("keys") or []
    rows = []
    prior_totals = _previous_totals_by_key(financial_year, keys)
    accounts = _accounts_for(financial_year, keys)

    # Whether last year's figure for a key can be put against ONE row or has
    # to be split between several - see _previous_by_account.
    shared = {}
    for account in accounts:
        shared[account.standard_key] = shared.get(account.standard_key, 0) + 1
    by_account = (_previous_by_account(financial_year, keys)
                  if any(n > 1 for n in shared.values()) else {})

    for account in accounts:
        amount = Decimal(str(account.net or 0))
        if amount < 0:
            amount = -amount
        if shared.get(account.standard_key, 0) > 1:
            # Sole claim on the key's total is what makes it this row's
            # figure. With siblings under the same key it has to be this
            # account's own, or nothing - never the shared total, which
            # would state each sibling at the combined figure.
            previous = by_account.get(
                (account.standard_key, _same_account(account.account_name)))
        else:
            previous = prior_totals.get(account.standard_key)
        rows.append(_row(account.account_name, amount, previous,
                         ref=f"tb:{account.id}"))

    if not rows:
        return None

    if "total" in spec and len(rows) > 1:
        label = spec["total"] if isinstance(spec["total"], str) else ""
        # Footed from the key totals rather than by adding the rows up. The
        # rows can hold the same key's figure more than once, or hold none
        # where one account of several could not be told from its siblings;
        # the key totals are the year's actual figure either way. Still only
        # footed when every key on show has one - a partial total would
        # misstate last year as complete.
        keys_shown = {a.standard_key for a in accounts}
        previous_total = (sum((prior_totals[k] for k in keys_shown), ZERO)
                          if keys_shown and all(k in prior_totals
                                                for k in keys_shown)
                          else None)
        rows.append(_row(label, sum(r["current"] for r in rows),
                         previous_total, bold=True, rule=True))

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


def _readable_heading(heading):
    """A table's caption, with the catalogue's own separator taken out.

    Fifty-three entries in notes_catalogue.yaml name the columns a
    disclosure has to show as a pipe-separated list - "Carrying amount |
    Fair value | Level within the fair value hierarchy". That is a
    separator for the catalogue, not punctuation for a reader, and it was
    reaching the page verbatim, where a run-on line of pipes above a
    table reads as something that failed to render rather than as a
    caption. Forty other entries write the same kind of content as an
    ordinary phrase ("Loss allowance: at 1 January, charge for the year,
    written off, at 31 December"), which is the register this matches.

    A middot rather than a comma because several of these items carry
    commas of their own, and comma-joining them runs two items together.
    """
    if not heading or "|" not in heading:
        return heading
    parts = [part.strip() for part in heading.split("|")]
    return " · ".join(part for part in parts if part)


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
            # Cleaned here rather than where the spec is first built, so
            # every report already created is corrected on its next render
            # instead of needing its stored data_binding rewritten.
            table["heading"] = _readable_heading(table.get("heading"))
            tables.append(table)

    return tables
