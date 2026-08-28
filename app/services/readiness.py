"""What is missing, said before the document is generated rather than after.

The firm's words: *"we should be told plainly which note cannot be completed
and what document or figure would complete it - not left to discover a blank
in the Word file after it has been produced."*

Nothing here is difficult. It is a list of what each part of the statements
needs, checked against what the engagement holds. Its value is entirely in
when it runs: before, so the preparer chases three documents once, rather
than after, so they read thirty pages hunting for holes.

A gap is never a reason to refuse to generate. Half a set of accounts with
the gaps named is more use than no accounts at all - the preparer often
knows the figure and will type it in. So this reports, and does not block.
"""
import logging

from ..models import Document, TrialBalanceAccount

log = logging.getLogger(__name__)


def _has_verified(financial_year, *categories):
    return any(d.category in categories and d.review_status == "verified"
               for d in financial_year.documents)


def _has_accounts(financial_year, *keys):
    return (TrialBalanceAccount.query
            .filter_by(financial_year_id=financial_year.id)
            .filter(TrialBalanceAccount.standard_key.in_(keys))
            .first() is not None)


def _previous_year(financial_year):
    from .outward import previous_year
    return previous_year(financial_year)


# Each requirement says what it serves, what would satisfy it, and - when it
# is not met - what to go and get. The last of those is the whole point:
# "missing" without "and here is what would fix it" is just a complaint.
REQUIREMENTS = [
    {
        "key": "comparatives",
        "serves": "Every statement's prior-year column",
        "needs": "Last year's figures",
        "get": ("Last year's signed accounts, uploaded and verified - or the "
                "previous financial year set up in Auditmate with its trial "
                "balance approved."),
        "why": ("Without them there is no second column at all. This is not "
                "a check that might flag a difference; the statements cannot "
                "be issued without it."),
        "test": lambda fy: (_previous_year(fy) is not None
                            or _has_verified(fy, "signed_accounts",
                                             "balance_sheet")),
    },
    {
        "key": "receivables_ageing",
        "serves": "Note 7 - Trade receivables, ageing table",
        "needs": "An aged receivables listing",
        "get": "The aged receivables report from the client's accounting system.",
        "why": ("The trial balance gives one figure for receivables. The "
                "ageing table needs it split by how old each balance is, "
                "which only the listing carries."),
        "test": lambda fy: (not _has_accounts(fy, "trade_receivables")
                            or _has_verified(fy, "receivables")),
    },
    {
        "key": "payables_listing",
        "serves": "Note 11 - Other payables",
        "needs": "An aged payables listing",
        "get": "The aged payables report from the client's accounting system.",
        "why": ("Needed to support the payables figure and to check it "
                "against the trial balance."),
        "test": lambda fy: (not _has_accounts(fy, "trade_payables")
                            or _has_verified(fy, "payables")),
    },
    {
        "key": "fixed_assets",
        "serves": "Fixed asset movement table",
        "needs": "A fixed asset register with cost, purchase date and useful life",
        "get": ("The client's fixed asset register. A figure for net book "
                "value is not enough - the table needs cost, additions, "
                "disposals and depreciation separately."),
        "why": ("Depreciation cannot be checked against an asset's useful "
                "life without knowing what that life is. On the engagement "
                "the firm tested, depreciation did not agree - and it "
                "balanced."),
        "test": lambda fy: (not _has_accounts(fy, "ppe")
                            or _has_verified(fy, "fixed_asset_register")),
    },
    {
        "key": "bank",
        "serves": "The cash figure, and Note 9",
        "needs": "A bank statement at the year end",
        "get": "The closing bank statement for every account the client holds.",
        "why": ("Cash is the one balance an outside party states "
                "independently. A trial balance with no bank account at all "
                "has happened, and the only symptom was an unexplained "
                "difference."),
        "test": lambda fy: (not _has_accounts(fy, "cash_and_equivalents")
                            or _has_verified(fy, "bank_statement")),
    },
    {
        "key": "tax",
        "serves": "Note 6 - Income tax expense",
        "needs": "The tax assessment or computation",
        "get": "The IRAS notice of assessment, or the tax computation.",
        "why": ("Tax expense and tax payable both need an outside figure to "
                "agree to. A tax payable balance that has not moved in a "
                "year is a finding this would surface."),
        "test": lambda fy: (not _has_accounts(fy, "tax_payable", "tax_expense")
                            or _has_verified(fy, "tax_document")),
    },
    {
        "key": "corporate_information",
        "serves": "Cover page, corporate information, directors' statement",
        "needs": "Directors, company secretary and registered office",
        "get": ("Fill these in on the client's record - Customers, then Edit. "
                "Directors and shareholdings are also needed at both the "
                "start and the end of the year."),
        "why": ("These pages are not generated from the trial balance. "
                "Without them the document opens with blanks on its first "
                "page."),
        "test": lambda fy: bool(
            (fy.customer.directors or "").strip()
            and (fy.customer.company_secretary or "").strip()
            and (fy.customer.address_line1 or "").strip()),
    },
    {
        "key": "uen",
        "serves": "Cover page and corporate information",
        "needs": "The company's UEN",
        "get": "Add the UEN on the client's record.",
        "why": "A Singapore set of accounts states it on the front page.",
        "test": lambda fy: bool((fy.customer.uen or "").strip()),
    },
]


def check(financial_year):
    """Everything the statements need, and whether the engagement has it.

    Returns {ready, have, missing, items}. `missing` is what to chase.
    """
    items = []
    for spec in REQUIREMENTS:
        try:
            met = bool(spec["test"](financial_year))
        except Exception:            # a broken check must not stop the page
            log.exception("Readiness check %s failed", spec["key"])
            met = False
        items.append({**{k: v for k, v in spec.items() if k != "test"},
                      "met": met})

    missing = [i for i in items if not i["met"]]
    return {
        "items": items,
        "missing": missing,
        "have": len(items) - len(missing),
        "total": len(items),
        "ready": not missing,
    }
