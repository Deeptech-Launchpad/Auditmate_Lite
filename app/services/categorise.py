"""Working out what a document is from what it is called.

The upload form carries one category for a whole batch, which is wrong the
moment a client sends the three documents they always send together: a
profit and loss, a balance sheet and a general ledger. One dropdown cannot
describe three things, so everything lands as "other" - and a document filed
as "other" builds nothing, because the builder does not know what it is
looking at. The error the auditor sees is "none of the verified documents can
build a trial balance", next to a list of a P&L, a balance sheet and a
ledger. It reads like a broken app.

The client's own file names already say it:

    DYNAMIC_ELECTRICAL_..._-_Profit_and_Loss.pdf
    DYNAMIC_ELECTRICAL_..._-_Balance_Sheet.pdf
    DYNAMIC_ELECTRICAL_..._-_General_Ledger_Detail.xlsx

An accounting system exporting a report names the file after the report. So
read the name.

This is a convenience, never an authority. It only ever runs when the
auditor has asked for it, whatever it decides is shown in the documents list
before anything is built from it, and it can be changed. A category decides
which document the accounts are built FROM - see models.TB_SOURCE_PRECEDENCE
- so a guess that stood unchallenged could quietly choose the wrong source.
Hence: never overrule an explicit choice, and fall back to "other" rather
than to a plausible-looking guess.
"""
import re

# Ordered, and the order matters. A file called "Trial Balance Summary" is a
# trial balance; one called "Balance Sheet" is not, despite both containing
# the word balance. The more specific phrase has to be tested first.
#
# Each pattern is matched against the file name with punctuation flattened to
# single spaces, so "Profit_and_Loss", "Profit-and-Loss" and "Profit & Loss"
# all read the same way.
RULES = [
    ("trial_balance", [
        r"\btrial balance\b", r"\btb\b", r"\btrialbalance\b",
    ]),
    ("general_ledger", [
        r"\bgeneral ledger\b", r"\bgl detail\b", r"\bledger detail\b",
        r"\bnominal ledger\b", r"\baccount transactions\b",
    ]),
    ("profit_and_loss", [
        r"\bprofit and loss\b", r"\bprofit loss\b", r"\bp and l\b",
        r"\bp l\b", r"\bincome statement\b", r"\bstatement of income\b",
        r"\bstatement of comprehensive income\b", r"\bprofitandloss\b",
    ]),
    ("balance_sheet", [
        r"\bbalance sheet\b", r"\bbalancesheet\b",
        r"\bstatement of financial position\b",
    ]),
    ("signed_accounts", [
        r"\bsigned accounts\b", r"\bfinancial statements\b",
        r"\bunaudited financial statements\b", r"\baudited accounts\b",
        r"\bstatutory accounts\b", r"\bannual report\b",
    ]),
    ("bank_statement", [
        r"\bbank statement\b", r"\bbank stmt\b", r"\bstatement of account\b",
    ]),
    ("receivables", [
        r"\baged receivable", r"\baccounts receivable\b", r"\bar aging\b",
        r"\bar ageing\b", r"\bdebtors\b",
    ]),
    ("payables", [
        r"\baged payable", r"\baccounts payable\b", r"\bap aging\b",
        r"\bap ageing\b", r"\bcreditors\b",
    ]),
    ("fixed_asset_register", [
        r"\bfixed asset", r"\basset register\b", r"\bdepreciation schedule\b",
    ]),
    ("salary_schedule", [
        r"\bpayroll\b", r"\bsalary\b", r"\bsalaries\b", r"\bcpf\b",
    ]),
    ("tax_document", [
        r"\btax computation\b", r"\bform c\b", r"\bgst\b", r"\biras\b",
        r"\btax return\b",
    ]),
]

_COMPILED = [(category, [re.compile(p) for p in patterns])
             for category, patterns in RULES]


def _flatten(filename: str) -> str:
    """A file name reduced to lower-case words separated by single spaces."""
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", filename or "")
    # An accounting system's export names are full of separators standing in
    # for spaces, and "&" standing in for "and".
    stem = stem.replace("&", " and ")
    stem = re.sub(r"[^A-Za-z0-9]+", " ", stem)
    return f" {stem.lower().strip()} "


def detect_category(filename: str) -> str:
    """The category a file name says it is, or "other" when it says nothing.

    "other" is the honest answer for a name that does not identify itself.
    The auditor then picks, which is the same work as today - but only for
    the files that genuinely need it.
    """
    haystack = _flatten(filename)
    if not haystack.strip():
        return "other"

    for category, patterns in _COMPILED:
        if any(pattern.search(haystack) for pattern in patterns):
            return category
    return "other"
