"""The mapping screen: every account, and where its statement line came from.

A trial balance arrives in the client's own words. The financial statements
have fixed line names. Something has to connect the two, and for a real
chart of accounts nothing automatic ever finishes the job - one tested
client mixes 4000-series codes with Exp-1 to Exp-14, CA-1 to CA-5 and CL-1
to CL-3, and no rule will ever guess what those hold. A person decides once,
and the decision has to survive to next year.

The machinery for that already exists: set_mapping writes the account and
calls learn_mapping, which stores a rule against the customer at priority 10
so it outranks every generic seed and is applied automatically on the next
build. What was missing is the screen - somewhere an auditor can see all
sixty accounts at once, with the unmapped ones first.

And one thing more, which is the point of this module rather than a plain
listing: **where each suggestion came from**. An auditor reviewing sixty
pre-filled lines needs to know which were carried forward from last year's
own decision, which a generic rule guessed, and which nothing could place.
Those three deserve different amounts of attention, and a screen that shows
them identically asks for all sixty to be checked equally - which means none
of them will be.
"""
import logging

from ..models import AccountMapping, TrialBalanceAccount
from .classify import classify
from .mapping import match_label
from .outward import previous_year

log = logging.getLogger(__name__)

# How a line got where it is. Ordered by how much attention it deserves.
ORIGIN_ORDER = {
    "unmapped": 0,       # nothing could place it. Needs a person.
    "suggested": 1,      # a generic rule guessed. Worth a glance.
    "carried": 2,        # this client's own decision, from a previous year.
    "manual": 3,         # decided here, in this engagement.
}

ORIGIN_LABELS = {
    "unmapped": "Not mapped",
    "suggested": "Suggested by a rule",
    "carried": "Carried from last year",
    "manual": "You mapped this",
}


def _last_year_map(financial_year):
    """account name (lowered) -> the line it was mapped to last year."""
    previous = previous_year(financial_year)
    if previous is None:
        return {}, None

    rows = (TrialBalanceAccount.query
            .filter_by(financial_year_id=previous.id)
            .filter(TrialBalanceAccount.standard_key.isnot(None))
            .all())
    return {(r.account_name or "").strip().lower(): r.standard_key
            for r in rows}, previous


def _origin(account, last_year, learned_patterns):
    """Where this account's mapping came from, and what to say about it."""
    name = (account.account_name or "").strip().lower()

    if not account.standard_key:
        return "unmapped", None

    # A person's own decision, made on this engagement, outranks everything.
    #
    # This used to read `and name not in learned_patterns`, which could never
    # be true: mapping an account writes the choice AND learns a rule for the
    # client in the same breath, so the account's own name was in the learned
    # set the instant it was mapped. "You mapped this" was therefore
    # unreachable, and every manual choice fell through to "Carried from last
    # year" - a claim that was simply false for an account like Exp-7, which
    # last year had never heard of.
    if account.mapping_is_manual:
        return "manual", None

    # Carried forward beats "suggested" wherever last year agrees, because
    # that is the stronger statement: this client, this account, already
    # decided - and consistency between the two years is what makes the
    # comparative column comparable in the first place.
    if last_year.get(name) == account.standard_key:
        return "carried", None

    if name in learned_patterns:
        return "carried", None

    return "suggested", None


def review(financial_year):
    """Every account with its line, its origin, and a suggestion if unmapped.

    Returns None when there is no trial balance yet - there is nothing to
    map before there are accounts.
    """
    accounts = (TrialBalanceAccount.query
                .filter_by(financial_year_id=financial_year.id)
                .order_by(TrialBalanceAccount.account_code,
                          TrialBalanceAccount.account_name)
                .all())
    if not accounts:
        return None

    last_year, previous = _last_year_map(financial_year)
    learned_patterns = {
        (r.pattern or "").lower()
        for r in AccountMapping.query
        .filter_by(customer_id=financial_year.customer_id).all()}

    rows = []
    for account in accounts:
        origin, _ = _origin(account, last_year, learned_patterns)
        entry = classify(account.standard_key) if account.standard_key else None

        # For an unmapped account, offer whatever the rules would have said
        # rather than an empty box. Last year first: this client already
        # answered this question once.
        suggestion = suggestion_from = None
        if origin == "unmapped":
            name = (account.account_name or "").strip().lower()
            if name in last_year:
                suggestion, suggestion_from = last_year[name], "last year"
            else:
                rule = match_label(account.account_name,
                                   financial_year.customer_id)
                if rule:
                    suggestion = rule["line_key"]
                    suggestion_from = ("last year"
                                       if rule.get("source") == "learned"
                                       else "the rule library")

        suggested_entry = classify(suggestion) if suggestion else None

        rows.append({
            "account": account,
            "origin": origin,
            "origin_label": ORIGIN_LABELS[origin],
            "line_label": entry["label"] if entry else None,
            "fs": entry["fs"] if entry else None,
            "suggestion": suggestion,
            "suggestion_label": (suggested_entry["label"]
                                 if suggested_entry else None),
            "suggestion_from": suggestion_from,
            "last_year": last_year.get(
                (account.account_name or "").strip().lower()),
        })

    # Unmapped first, then rule guesses, then what is already settled. The
    # work is at the top of the page and the auditor stops scrolling when it
    # runs out.
    rows.sort(key=lambda r: (ORIGIN_ORDER[r["origin"]],
                             r["account"].account_code or "",
                             r["account"].account_name or ""))

    counts = {k: 0 for k in ORIGIN_ORDER}
    for row in rows:
        counts[row["origin"]] += 1

    return {
        "rows": rows,
        "counts": counts,
        "total": len(rows),
        "previous": previous,
        "has_suggestions": any(r["suggestion"] for r in rows),
        # An account nothing can place is the finding this screen exists for.
        # Until it is mapped it reaches no statement at all, so a new bank
        # account or a new liability would simply be absent from the accounts
        # with nothing announcing it.
        "unmapped": counts["unmapped"],
    }


def apply_suggestions(financial_year, user_id=None):
    """Accept every suggestion on unmapped accounts. Returns how many.

    Deliberately only touches accounts that are unmapped. It never revisits
    something already decided, so pressing it twice is safe and it can never
    undo an auditor's own choice.
    """
    from .trial_balance import set_mapping

    data = review(financial_year)
    if not data:
        return 0

    applied = 0
    for row in data["rows"]:
        if row["origin"] != "unmapped" or not row["suggestion"]:
            continue
        result = set_mapping(row["account"].id, row["suggestion"],
                             user_id=user_id)
        if result.get("ok"):
            applied += 1
        else:
            log.warning("Could not apply suggestion for account %s: %s",
                        row["account"].id, result.get("error"))
    return applied
