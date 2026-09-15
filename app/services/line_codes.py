"""Each trial balance account's notes library line code.

An account already carries AuditMate's statement line (standard_key), which
builds the face of the statements. Library 2.x builds its notes from a finer
vocabulary: every table row names the line code it takes a figure from, and
composite rows add several. So an account needs a line code too, and it has
to be the most specific one that applies - a deposit is BS-DEP, never the
catch-all BS-OR, or the other receivables total counts it wrongly.

config/line_code_categories.yaml says which line codes may sit under each
statement line. This decides one per account, and says how sure it is:

    only      the statement line allows one code          settled
    carried   the same account, settled last year          settled
    manual    a person chose it                            settled
    rule      proposed from the account's name or side     a proposal
    default   proposed as the line's usual code            a proposal
    (none)    several codes fit and nothing decides        ask the preparer

A proposal stays a proposal. Nothing here promotes a guess into a decision,
including across years: a category is carried forward only when last year's
was settled, because carrying a proposal forward would present last year's
guess as this year's decision - exactly the trap the mapping screen's own
"Carried from last year" label fell into when a prior year was mapped by
rule guesses.

Nothing here calls the AI.
"""
import logging
import re

import yaml
from flask import current_app

from ..extensions import db
from ..models import NoteLibraryVersion, TrialBalanceAccount

log = logging.getLogger(__name__)

SETTLED = {"only", "carried", "manual"}
PROPOSED = {"rule", "default"}


def load_categories():
    """The category file, read fresh so an edit takes effect immediately."""
    path = current_app.config["CONFIG_DIR"] / "line_code_categories.yaml"
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return (yaml.safe_load(handle) or {}).get("lines") or {}


def known_codes(financial_year=None):
    """Line codes the engagement's library version defines, or None.

    Library 1.0 has no Statement lines sheet, so an engagement still on it is
    checked against the newest version that has one. None means no loaded
    version defines line codes at all, and nothing is checked.
    """
    version = None
    if financial_year is not None and financial_year.library_version_id:
        version = db.session.get(NoteLibraryVersion,
                                 financial_year.library_version_id)
    if version is None or not version.sheet("Statement lines"):
        version = next((v for v in NoteLibraryVersion.query
                        .order_by(NoteLibraryVersion.imported_at.desc()).all()
                        if v.sheet("Statement lines")), None)
    if version is None:
        return None
    return {r.get("Line code") for r in version.sheet("Statement lines")
            if r.get("Line code")}


def allowed_codes(standard_key, categories=None):
    """The line codes that may sit under one statement line, in file order."""
    categories = load_categories() if categories is None else categories
    spec = categories.get(standard_key) or {}
    return list(spec.get("codes") or [])


def _side(account):
    net = (account.debit or 0) - (account.credit or 0)
    if net > 0:
        return "debit"
    if net < 0:
        return "credit"
    return None


def _rule_fits(rule, name, side):
    if rule.get("side") and rule["side"] != side:
        return False
    pattern = rule.get("match")
    if pattern and not re.search(pattern, name, flags=re.IGNORECASE):
        return False
    return bool(rule.get("side") or pattern)


def last_year_settled(financial_year):
    """{(account name, standard_key): line_code} settled in the prior year."""
    if not financial_year or not financial_year.previous_year_id:
        return {}
    settled = {}
    for account in (TrialBalanceAccount.query
                    .filter_by(financial_year_id=financial_year.previous_year_id)
                    .filter(TrialBalanceAccount.line_code.isnot(None)).all()):
        if account.line_code_source in SETTLED:
            key = ((account.account_name or "").strip().lower(),
                   account.standard_key)
            settled[key] = account.line_code
    return settled


def propose(account, categories, last_year=None, codes=None):
    """(line_code, source) for one account, or (None, None) to ask.

    Does not write. `codes` limits the answer to codes the library version
    defines; a category file entry the version does not know is never used.
    """
    if not account.standard_key:
        return None, None
    options = [c for c in allowed_codes(account.standard_key, categories)
               if codes is None or c in codes]
    if not options:
        return None, None
    if len(options) == 1:
        return options[0], "only"

    name = (account.account_name or "").strip()
    carried = (last_year or {}).get((name.lower(), account.standard_key))
    if carried in options:
        return carried, "carried"

    spec = categories.get(account.standard_key) or {}
    side = _side(account)
    for rule in spec.get("split") or []:
        if rule.get("code") in options and _rule_fits(rule, name, side):
            return rule["code"], "rule"

    if spec.get("default") in options:
        return spec["default"], "default"
    return None, None


def assign(account, categories=None, last_year=None, codes=None):
    """Set one account's line code. Returns True if it changed.

    A person's choice is kept for as long as it is still allowed under the
    account's statement line. Once the statement line itself changes - the
    preparer remapped the account - a manual code for the old line no longer
    means anything and is re-decided rather than left pointing at the wrong
    note.
    """
    categories = load_categories() if categories is None else categories
    before = (account.line_code, account.line_code_source)

    if account.line_code_source == "manual":
        if (account.standard_key
                and account.line_code in allowed_codes(account.standard_key,
                                                       categories)
                and (codes is None or account.line_code in codes)):
            return False

    code, source = propose(account, categories, last_year, codes)
    account.line_code, account.line_code_source = code, source
    return before != (code, source)


def choose(account, code, categories=None, codes=None):
    """Record a person's choice. Raises ValueError if it is not allowed."""
    categories = load_categories() if categories is None else categories
    options = allowed_codes(account.standard_key, categories)
    if code not in options:
        raise ValueError(
            f"{code} is not a category for this account's statement line. "
            f"Allowed: {', '.join(options) or 'none'}")
    if codes is not None and code not in codes:
        raise ValueError(f"{code} is not defined by this engagement's notes "
                         f"library version")
    account.line_code, account.line_code_source = code, "manual"


def assign_year(financial_year, commit=True):
    """Decide the line code of every account in one engagement. Returns a
    summary: counts by source, and the accounts still needing a choice."""
    categories = load_categories()
    codes = known_codes(financial_year)
    last_year = last_year_settled(financial_year)

    counts = {"only": 0, "carried": 0, "manual": 0, "rule": 0, "default": 0,
              "ask": 0, "unmapped": 0}
    needs_choice = []
    for account in (TrialBalanceAccount.query
                    .filter_by(financial_year_id=financial_year.id)
                    .order_by(TrialBalanceAccount.account_code,
                              TrialBalanceAccount.account_name).all()):
        assign(account, categories, last_year, codes)
        if not account.standard_key:
            counts["unmapped"] += 1
        elif account.line_code_source:
            counts[account.line_code_source] += 1
        else:
            counts["ask"] += 1
            needs_choice.append({
                "account": account.account_name,
                "standard_key": account.standard_key,
                "options": allowed_codes(account.standard_key, categories)})
    if commit:
        db.session.commit()
    return {"counts": counts, "needs_choice": needs_choice}
