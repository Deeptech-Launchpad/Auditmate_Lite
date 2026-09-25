"""Maps extracted account labels onto financial statement lines.

Three tiers, cheapest first:

  1. Customer-specific learned rules  — remembered from past corrections
  2. Global seed rules (YAML)         — deterministic, covers common labels
  3. AI classification (Claude)       — only for labels nothing else matched

Whatever isn't matched by any tier lands in the auditor's "Unmapped items"
tray. When the auditor assigns a line there, we save that choice as a
customer-scoped rule, so the same client's next financial year maps
automatically. That's what makes the tool get faster with use.
"""
import functools
import logging
import re

import yaml
from flask import current_app

from ..extensions import db
from ..models import AccountMapping

log = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _seed_rules():
    """Load the global YAML rules once per process."""
    path = current_app.config["CONFIG_DIR"] / "mapping_rules_default.yaml"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or []

    rules = []
    for entry in raw:
        rules.append({
            "pattern": str(entry.get("pattern", "")).lower(),
            "match_type": entry.get("match", "contains"),
            "statement_type": entry.get("statement"),
            "line_key": entry.get("line"),
            "sign": int(entry.get("sign", 1)),
            "priority": int(entry.get("priority", 100)),
            "source": "seed",
        })
    return sorted(rules, key=lambda r: r["priority"])


def _matches(rule, label: str) -> bool:
    pattern = rule["pattern"]
    if not pattern:
        return False
    match_type = rule.get("match_type", "contains")

    if match_type == "exact":
        return label == pattern
    if match_type == "regex":
        try:
            return re.search(pattern, label, flags=re.IGNORECASE) is not None
        except re.error:
            return False
    return pattern in label


def _customer_rules(customer_id: int):
    """Learned + manual rules for one customer, highest precedence first."""
    rows = (AccountMapping.query
            .filter(AccountMapping.customer_id == customer_id)
            .order_by(AccountMapping.priority.asc())
            .all())
    return [{
        "pattern": (r.pattern or "").lower(),
        "match_type": r.match_type or "contains",
        "statement_type": r.statement_type,
        "line_key": r.line_key,
        "sign": r.sign or 1,
        "priority": r.priority or 50,
        "source": r.source or "learned",
    } for r in rows]


# Words in an account-type heading that settle which statement it belongs to.
# Balance sheet words are tested first: "Unearned revenue" and "Income tax
# payable" are liabilities, whatever else the heading says.
_BALANCE_SHEET_TYPE_WORDS = ("asset", "liabilit", "equity", "bank", "payable",
                             "receivable", "prepayment", "inventory",
                             "credit card", "unearned", "deferred", "capital",
                             "accumulated")
_PROFIT_AND_LOSS_TYPE_WORDS = ("revenue", "income", "sales", "expense",
                               "overhead", "cost of", "direct cost",
                               "depreciation", "cogs")


def statement_for_account_type(account_type):
    """Which statement an account belongs to, judged by the type its books
    give it - or None when the type does not say.

    Accounting software classifies every account when it is created: Xero's
    "Expense", "Current Liability", "Fixed Asset"; QuickBooks' "Other
    Current Liabilities", "Cost of Goods Sold". That classification is the
    most reliable single fact about an account, far more than its name,
    which is whatever the bookkeeper typed.

    None is the common answer, and it is safe: an account with no type, or a
    type this does not recognise, is matched exactly as before.
    """
    text = (account_type or "").strip().lower()
    if not text:
        return None
    if any(word in text for word in _BALANCE_SHEET_TYPE_WORDS):
        return "balance_sheet"
    if any(word in text for word in _PROFIT_AND_LOSS_TYPE_WORDS):
        return "profit_and_loss"
    return None


# Which SIDE of the balance sheet an account's own stated type puts it on.
# Narrower than _BALANCE_SHEET_TYPE_WORDS above, and answering a different
# question: that list decides P&L against balance sheet; this one decides
# asset against liability against equity, which "balance sheet" alone
# cannot - Xero's own type names say it plainly ("Current Asset", "Current
# Liability"), so this reads them the same direct way.
_ASSET_TYPE_WORDS = ("current asset", "fixed asset", "non-current asset",
                     "non current asset", "other asset", "inventory")
_LIABILITY_TYPE_WORDS = ("current liability", "non-current liability",
                         "non current liability", "long-term liability",
                         "long term liability", "other liability")
_EQUITY_TYPE_WORDS = ("equity",)


def side_for_account_type(account_type):
    """asset / liability / equity, read from the account's own stated
    type - or None when the type does not say or does not distinguish.

    "Balance sheet" is not narrow enough on its own: an asset account and
    a liability account are both on it, so a rule can be on the right
    STATEMENT and still land an account on the wrong SIDE of it. That is
    what let "Amount Due from Director", typed Current Liability by the
    client's own books, match a rule written for money owed TO a
    company - a liability read onto an asset line, printed as a
    negative deduction from receivables, until nothing in the accounts
    balanced any more. The client's own type already said which side;
    nothing was reading it at this resolution before.
    """
    text = (account_type or "").strip().lower()
    if not text:
        return None
    if any(word in text for word in _LIABILITY_TYPE_WORDS):
        return "liability"
    if any(word in text for word in _EQUITY_TYPE_WORDS):
        return "equity"
    if any(word in text for word in _ASSET_TYPE_WORDS):
        return "asset"
    return None


_ASSET_GROUPS = {"current_assets", "non_current_assets", "assets_total"}
_LIABILITY_GROUPS = {"current_liabilities", "non_current_liabilities",
                     "liabilities_total"}
_EQUITY_GROUPS = {"equity"}


def _side_of_line(line_key):
    """asset / liability / equity for a STATEMENT LINE - a candidate rule's
    destination, not an account's own type. Deferred import: classify.py
    imports statements.py, which imports this module back for _signed(),
    the same reason that import is already deferred there.
    """
    from .classify import classify

    entry = classify(line_key)
    group = (entry or {}).get("group")
    if group in _LIABILITY_GROUPS:
        return "liability"
    if group in _EQUITY_GROUPS:
        return "equity"
    if group in _ASSET_GROUPS:
        return "asset"
    return None


def standard_class(account_type):
    from . import standard_lines

    return standard_lines.account_class(account_type)


def _standard_rule(label, account_type):
    """(rule or None, decided) from the client's standard statement lines.

    decided is True when they settle the account - placed on a line, or sent to
    the accountant because it is a tie or on the never-place list - and False
    when they have nothing to say and the older rules may still be asked.
    """
    from . import standard_lines as sl

    if not sl.load().get("lines"):
        return None, False
    if sl.never_auto(label):
        return None, True
    klass = sl.account_class(account_type)
    if klass is None:
        return None, False
    result, detail = sl.place(label, klass)
    if result == "tie":
        log.info("Account %r ties between %s - left to the accountant", label,
                 ", ".join(h[0]["key"] for h in detail))
        return None, True
    if result != "placed":
        # Rule 5: nothing recognised. An expense becomes Other expenses (the
        # caller adds it, flagged); anything else goes to the accountant.
        # Decided either way - the older rules are not asked once the class
        # is known, or a stray word ("charg") would overrule the client's list.
        if klass == "pl_expense":
            return ({"pattern": "", "match_type": "contains",
                     "statement_type": "profit_and_loss", "line_key": "other_expenses",
                     "sign": 1, "priority": 999, "source": "standard_review"}, True)
        return None, True
    line, keyword = detail
    return ({"pattern": keyword, "match_type": "contains",
             "statement_type": line["statement"], "line_key": sl.app_key(line["key"]),
             "sign": 1, "priority": 0, "source": "standard"}, True)


def match_label(label: str, customer_id: int, statement_type: str = None,
                account_type: str = None):
    """Find the best rule for one label without calling the AI.

    The winning rule is chosen across *all* statements by priority, then
    checked against the statement being built. This ordering matters: an
    account belongs to exactly one statement line, and picking the best rule
    per-statement instead would let a greedy low-priority rule claim an
    account that a sharper rule already owns elsewhere.

    Concretely, "Bank Charges" is matched by both `bank charge` (finance
    costs, priority 15) and the catch-all `bank` (cash, priority 30). Choosing
    globally means finance costs wins outright, so the amount lands in the
    P&L only. Choosing per-statement would put it in the P&L *and* in cash on
    the balance sheet -- counting the same figure twice and unbalancing the
    accounts.

    ACCOUNT TYPE, WHEN GIVEN, OVERRULES THE NAME. A rule on the wrong
    statement for the account's type is passed over and the next match is
    tried. The same word means different things: an account called "GST"
    typed Expense is GST the company paid and could not reclaim; one typed
    Current Liability is GST it owes. Matching the name alone put a
    S$36,225 expense onto the balance sheet as GST payable, and the year's
    tax charge onto tax payable - on the first real client file loaded.

    Every caller holding an account should pass its type, and not only the
    one that maps it: the sign a figure is presented with is read from the
    same rule. A mapping that honoured the type while the sign lookup did
    not would place an expense correctly and then print it negative.

    Returns the matching rule dict, or None when the label matches nothing or
    belongs to a different statement.
    """
    if not label:
        return None
    normalised = label.lower().strip()
    side = statement_for_account_type(account_type)
    bs_side = side_for_account_type(account_type)

    def fits(rule):
        if side is not None and rule["statement_type"] != side:
            return False
        # Only meaningful on the balance sheet - a P&L rule has no
        # asset/liability side of its own to disagree with, and
        # checking bs_side there would refuse every one of them the
        # moment an account's type happened to say "Expense".
        if bs_side is not None and rule["statement_type"] == "balance_sheet":
            rule_side = _side_of_line(rule["line_key"])
            if rule_side is not None and rule_side != bs_side:
                return False
        return True

    # Customer rules first -- a correction made for this client beats a
    # generic seed rule every time. Both lists are already priority-sorted,
    # so the first match in each is the best one.
    best = None
    for rule in _customer_rules(customer_id):
        if _matches(rule, normalised) and fits(rule):
            best = rule
            break

    if best is None:
        # The client's standard statement lines, when the account's class is
        # known: class first, longest keyword, a tie or a never-place name goes
        # to the accountant. See services/standard_lines.py.
        standard, decided = _standard_rule(label, account_type)
        if decided:
            return standard if (standard and (
                not statement_type or standard["statement_type"] == statement_type)) else None

    if best is None:
        for rule in _seed_rules():
            if _matches(rule, normalised) and fits(rule):
                best = rule
                break

    if best is None and standard_class(account_type) == "pl_expense":
        # Rule 5 of the standard lines: an expense nothing recognises is Other
        # expenses, and is flagged for a glance.
        best = {"pattern": "", "match_type": "contains",
                "statement_type": "profit_and_loss", "line_key": "other_expenses",
                "sign": 1, "priority": 999, "source": "standard_review"}

    if best is None:
        return None

    if statement_type and best["statement_type"] != statement_type:
        return None

    return best


# Ageing buckets, matched locally rather than through the global rules.
#
# Receivable and payable listings use word-for-word identical bucket labels
# ("31 - 60 days overdue"), so a global rule cannot tell which statement a
# given row belongs to - whichever rule was written first would swallow both.
# What actually distinguishes them is the document's category, and by the time
# we get here the caller has already filtered to the right documents. So for
# these two statements the bucket is resolved from the label alone, scoped to
# the statement being built.
AGEING_PATTERNS = [
    (("not yet due", "current"), "current"),
    (("1 - 30", "1-30", "0 - 30", "0-30", "30 day"), "1_30"),
    (("31 - 60", "31-60"), "31_60"),
    (("61 - 90", "61-90"), "61_90"),
    (("over 90", "90+", "more than 90", "above 90"), "over_90"),
    (("allowance", "doubtful", "provision for bad"), "provision"),
]


def match_ageing_bucket(label: str, statement_type: str):
    """Resolve an ageing-listing label to its bucket line key, or None."""
    prefix = {"accounts_receivable": "ar", "accounts_payable": "ap"}.get(
        statement_type)
    if not prefix or not label:
        return None

    text = label.lower().strip()
    # Longest/most specific ranges first so "over 90" is not shadowed by a
    # looser "90 day" match.
    for needles, suffix in AGEING_PATTERNS:
        if any(n in text for n in needles):
            return f"{prefix}_{suffix}"
    return None


def map_line_items(line_items, customer_id: int, statement_type: str,
                   valid_line_keys: list, use_ai: bool = True):
    """Map a batch of extracted line items onto statement lines.

    Returns (mapped, unmapped) where mapped is
    {line_key: [(line_item, sign), ...]}.
    """
    mapped = {}
    unmatched = []

    # Ageing statements resolve their own buckets (see AGEING_PATTERNS).
    if statement_type in ("accounts_receivable", "accounts_payable"):
        for item in line_items:
            key = match_ageing_bucket(item.label, statement_type)
            if key and key in valid_line_keys:
                sign = -1 if key.endswith("_provision") else 1
                mapped.setdefault(key, []).append((item, sign))
            else:
                unmatched.append(item)
        return mapped, unmatched

    # --- Tier 1 + 2: deterministic rules -----------------------------------
    for item in line_items:
        rule = match_label(item.label, customer_id, statement_type,
                           account_type=getattr(item, "account_type", None))

        if rule and rule["line_key"] in valid_line_keys:
            mapped.setdefault(rule["line_key"], []).append((item, rule["sign"]))
            continue

        if rule:
            # A rule claimed this label for THIS statement, but points at a
            # line key the template no longer has - typically because the
            # statement layout was edited and the rule was left behind.
            # Surface it as unmapped rather than dropping it: a figure that
            # silently vanishes from the accounts is the worst possible
            # failure mode here.
            log.warning("Mapping rule %r targets unknown line %r on %s - "
                        "sending %r to the unmapped tray",
                        rule["pattern"], rule["line_key"], statement_type,
                        item.label)
            unmatched.append(item)
            continue

        # No rule for this statement. Before calling it unmapped, check
        # whether it belongs to a different statement - a revenue account is
        # not "unmapped" just because we're building the balance sheet.
        if match_label(item.label, customer_id,
                       account_type=getattr(item, "account_type", None)) is None:
            unmatched.append(item)

    # --- Tier 3: AI, only for what's left ----------------------------------
    if unmatched and use_ai:
        from .extraction.ai import ai_available, classify_accounts

        if ai_available():
            # De-duplicate labels so we don't pay to classify the same string
            # twenty times.
            labels = sorted({(i.label or "").strip()
                             for i in unmatched if (i.label or "").strip()})
            if labels:
                log.info("Asking AI to classify %d unmatched labels", len(labels))
                suggestions = classify_accounts(labels, valid_line_keys,
                                                statement_type)

                still_unmatched = []
                for item in unmatched:
                    suggestion = suggestions.get((item.label or "").strip())
                    # Only trust a confident AI mapping; anything shakier goes
                    # to the auditor rather than silently into the accounts.
                    if (suggestion
                            and suggestion["line_key"] in valid_line_keys
                            and suggestion["confidence"] >= 0.7):
                        mapped.setdefault(suggestion["line_key"], []).append((item, 1))
                    else:
                        still_unmatched.append(item)
                unmatched = still_unmatched

    return mapped, unmatched


def learn_mapping(customer_id: int, label: str, statement_type: str,
                  line_key: str, user_id: int = None) -> AccountMapping:
    """Remember an auditor's manual mapping decision for this customer."""
    pattern = (label or "").strip().lower()
    if not pattern:
        return None

    existing = AccountMapping.query.filter_by(
        customer_id=customer_id, pattern=pattern,
        statement_type=statement_type).first()

    if existing:
        existing.line_key = line_key
        return existing

    rule = AccountMapping(
        customer_id=customer_id,
        pattern=pattern,
        match_type="exact",
        statement_type=statement_type,
        line_key=line_key,
        sign=1,
        priority=10,          # learned rules outrank generic seeds
        source="learned",
        created_by=user_id,
    )
    db.session.add(rule)
    return rule
