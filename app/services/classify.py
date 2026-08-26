"""Which statement an account belongs to, and under what heading.

The trial balance grid shows an auditor three things about every account:
the statement it lands in (FS), the category it sits under there, and the
line it maps to. Only the last of those is a choice - the other two follow
from it, so they are derived here rather than stored, and cannot drift out
of step with the mapping.

Everything comes from config/statement_templates.yaml: the account's
standard_key finds its line, the line carries its `group`, and the group is
given a plain-English name below.
"""
import functools

from .statements import load_templates

# Statement type -> what an auditor calls it in a trial balance listing.
FS_LABELS = {
    "profit_and_loss": "P&L",
    "balance_sheet": "Balance Sheet",
    "cash_flow": "Cash Flow",
    "changes_in_equity": "Changes in Equity",
    "accounts_receivable": "Balance Sheet",
    "accounts_payable": "Balance Sheet",
}

# Statement group -> the category column. These are the headings a Singapore
# SME trial balance is normally grouped under, not the internal group keys.
GROUP_LABELS = {
    "revenue": "Revenue",
    "cost_of_sales": "Cost of Sales",
    "gross": "Cost of Sales",
    "operating_expenses": "Expenses",
    "result": "Tax",
    "non_current_assets": "Fixed Asset",
    "current_assets": "Current Asset",
    "assets_total": "Current Asset",
    "equity": "Equity",
    "non_current_liabilities": "Long-term Liability",
    "current_liabilities": "Current Liability",
    "liabilities_total": "Current Liability",
    "ageing": "Current Asset",
}

# A handful of lines an auditor expects to see named specifically, rather
# than under the broad heading of their group.
LINE_LABELS = {
    "prepayments": "Asset - Other Receivables",
    "trade_receivables": "Asset - Trade Receivables",
    "cash_and_equivalents": "Cash & Bank",
    "inventories": "Inventory",
    "share_capital": "Share Capital",
    "working_capital": "Share Capital",
    "retained_earnings": "Accumulated Profit",
    "trade_payables": "Liability - Payables",
    "accruals": "Liability - Accruals",
    "tax_payable": "Tax",
    "tax_expense": "Tax",
    "ppe": "Fixed Asset",
    "long_term_borrowings": "Borrowings",
    "short_term_borrowings": "Borrowings",
}


# Groups whose accounts normally carry a credit balance. A printed statement
# has a single amount column and no sides - revenue, payables and share
# capital are all shown as positive numbers - so this is what decides which
# side such a figure belongs on.
CREDIT_BALANCE_GROUPS = {
    "revenue",
    "equity",
    "current_liabilities",
    "non_current_liabilities",
    "liabilities_total",
}


@functools.lru_cache(maxsize=1)
def _index():
    """standard_key -> {fs, category, label, group}, built from the templates."""
    index = {}
    for statement_type, spec in load_templates().items():
        if statement_type == "trial_balance":
            continue
        for line in (spec.get("lines") or []):
            key = line.get("key")
            # A subtotal is not something an account maps to, and the first
            # statement to define a key wins - profit_and_loss and
            # balance_sheet never share one.
            if not key or line.get("subtotal") or line.get("total"):
                continue
            index.setdefault(key, {
                "fs": FS_LABELS.get(statement_type, statement_type),
                "category": LINE_LABELS.get(
                    key, GROUP_LABELS.get(line.get("group"), "Other")),
                "label": line.get("label", key),
                "group": line.get("group"),
            })
    return index


def is_credit_balance(standard_key) -> bool:
    """Whether a mapped account normally sits on the credit side.

    Taken from the statement templates rather than from a mapping rule's
    `sign`, because a learned rule is written with sign=1 whatever the
    account is, and several seed rules omit it. The template's group is the
    one place that is always right.
    """
    entry = _index().get(standard_key or "")
    return bool(entry and entry.get("group") in CREDIT_BALANCE_GROUPS)


def classify(standard_key):
    """Return {fs, category, label} for a mapped account, or None."""
    if not standard_key:
        return None
    return _index().get(standard_key)
