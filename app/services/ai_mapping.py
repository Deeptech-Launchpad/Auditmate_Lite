"""A model's opinion on the accounts nothing else could place.

Most of a chart maps itself. Last year's decisions carry, the category
rules catch the ordinary names, and what is left is the handful nobody
has seen before - "Exp-7", "Retention - Sembcorp", "Sundry 2". Those go
to a person, and this offers them an opinion first.

WHAT LEAVES THE MACHINE, AND WHEN

Nothing, unless somebody has turned it on for that client. `ai_allowed`
is off until a person sets it, for one client at a time, and this
refuses rather than asks again. The two mistakes are not symmetric:
forgetting to switch it on costs somebody twenty minutes with a
dropdown, and forgetting to switch it off means a real client's books
went to a third party, which cannot be undone.

What goes, when it goes, is the least that could answer the question: an
account name, and whether it sits on the debit or credit side. Not the
amount - the balance says what the company is worth, and naming a line
does not need it. Not the client's name, not its UEN, not the year. A
list of account names with no figures and no company attached is a
weaker thing to send than a trial balance, and it is all the question
takes.

WHAT COMES BACK IS A SUGGESTION AND STAYS ONE

Nothing here writes a mapping. A suggestion is recorded against the
account with the model that gave it and the reason it gave, and a person
accepts it or does not. The origin stays visible afterwards, so a
reviewer a year later can see which lines a model proposed rather than
finding them indistinguishable from the firm's own decisions.

The model is also told it may decline. An honest "I do not know" for
"Sundry 2" is worth more than a confident guess, because a confident
guess is what a preparer accepts without reading.
"""
import json
import logging

from ..extensions import db
from ..models import AiMappingSuggestion, TrialBalanceAccount  # noqa: F401
from . import line_codes

log = logging.getLogger(__name__)


class NotPermitted(RuntimeError):
    """This client has not been cleared for anything to leave the machine."""


class NotAvailable(RuntimeError):
    """No model is configured, or it could not be reached."""


# Names that carry a person or a counterparty rather than a category.
# Not a filter - everything goes or nothing does - but worth counting, so
# the confirmation screen can say what is about to be sent and how much
# of it looks like somebody's name.
PERSONAL_HINTS = ("loan", "director", "advance", "due to", "due from",
                  "retention", "deposit")

MAX_ACCOUNTS = 60


def permitted(customer):
    """Whether anything about this client may go to a model at all."""
    return bool(getattr(customer, "ai_allowed", False))


def unmapped(financial_year):
    """The accounts no rule and no previous year could place."""
    return (TrialBalanceAccount.query
            .filter_by(financial_year_id=financial_year.id)
            .filter(TrialBalanceAccount.standard_key.is_(None))
            .order_by(TrialBalanceAccount.account_code,
                      TrialBalanceAccount.account_name)
            .all())


def _side(account):
    """Debit or credit, which is most of what distinguishes two lines."""
    if account.debit:
        return "debit"
    if account.credit:
        return "credit"
    return "debit"


def outgoing(financial_year):
    """Exactly what would be sent, so a person can read it before it goes.

    The whole payload, not a description of it. A screen that says "account
    names will be sent" asks to be believed; one that shows the list can be
    checked.
    """
    rows = []
    for account in unmapped(financial_year)[:MAX_ACCOUNTS]:
        name = (account.account_name or "").strip()
        rows.append({
            "id": account.id,
            "name": name,
            "side": _side(account),
            "looks_personal": any(hint in name.lower()
                                  for hint in PERSONAL_HINTS),
        })
    return rows


def _codes(financial_year):
    """The lines a suggestion is allowed to name, as the library defines them."""
    labels = line_codes.code_labels(financial_year) or {}
    return [{"code": code, "label": label} for code, label in
            sorted(labels.items())]


_SYSTEM = (
    "You map accounts from a Singapore company's trial balance onto the "
    "line codes of a financial reporting notes library. You are given "
    "account names and whether each sits on the debit or credit side, and "
    "the list of line codes you may choose from.\n\n"
    "Answer only with codes from that list. Where an account name does "
    "not tell you enough to choose - a name like \"Sundry 2\" or "
    "\"Exp-7\" - return no code for it and say what you would need to "
    "know. A wrong answer given confidently is worse than no answer, "
    "because a person will accept it without checking.\n\n"
    "Give a short reason for each code you do choose, naming the part of "
    "the account name you read it from."
)


def _schema():
    """The shape a reply must take, built where the provider wants it."""
    from pydantic import BaseModel, Field

    class One(BaseModel):
        name: str = Field(description="the account name, exactly as given")
        code: str = Field(default="", description="a line code, or empty")
        reason: str = Field(default="", description="why, in one sentence")

    class Reply(BaseModel):
        mappings: list[One] = Field(default_factory=list)

    return Reply


def suggest(financial_year, user_id=None):
    """Ask a model about the unplaced accounts. Returns what it said.

    Raises rather than proceeding where the client has not been cleared:
    an exception is harder to ignore than an empty list, and this is the
    one check in the application where quietly doing nothing and quietly
    doing it anyway look the same from the outside.
    """
    customer = financial_year.customer
    if not permitted(customer):
        raise NotPermitted(
            f"{customer.name} has not been allowed to have anything sent "
            f"to a model. Nothing was sent.")

    accounts = outgoing(financial_year)
    if not accounts:
        return []

    from .extraction.providers import get_provider, provider_name

    provider = get_provider()
    if not provider.available():
        raise NotAvailable("No model is configured.")

    payload = {
        "accounts": [{"name": row["name"], "side": row["side"]}
                     for row in accounts],
        "line_codes": _codes(financial_year),
    }
    reply = provider.structured_call(
        _SYSTEM, [{"type": "text", "text": json.dumps(payload, indent=1)}],
        _schema(), max_tokens=4000)

    by_name = {row["name"]: row["id"] for row in accounts}
    known = {entry["code"] for entry in payload["line_codes"]}
    model = getattr(provider, "model_name", lambda: provider_name())()

    out = []
    for item in reply.mappings:
        account_id = by_name.get((item.name or "").strip())
        if account_id is None:
            continue
        code = (item.code or "").strip()
        # A code the library does not define is not a suggestion, it is
        # noise. Dropped rather than shown, so nobody accepts one.
        if code and code not in known:
            log.info("Model proposed unknown line code %r", code)
            code = ""
        out.append(record(financial_year, account_id, code,
                          (item.reason or "").strip(), model,
                          user_id=user_id, commit=False))
    db.session.commit()
    return [row for row in out if row is not None]


def record(financial_year, account_id, code, reason, model, user_id=None,
           commit=True):
    """Keep what the model said against the account it said it about."""
    row = AiMappingSuggestion.query.filter_by(
        financial_year_id=financial_year.id, account_id=account_id).first()
    if row is None:
        row = AiMappingSuggestion(financial_year_id=financial_year.id,
                                  account_id=account_id)
        db.session.add(row)
    row.code = code or None
    row.reason = reason or None
    row.model = model
    row.requested_by = user_id
    row.decision = None
    row.decided_by = None
    row.decided_at = None
    if commit:
        db.session.commit()
    return row


def pending(financial_year):
    """Suggestions nobody has accepted or rejected yet."""
    return (AiMappingSuggestion.query
            .filter_by(financial_year_id=financial_year.id)
            .filter(AiMappingSuggestion.decision.is_(None))
            .filter(AiMappingSuggestion.code.isnot(None)).all())


def accept(suggestion, user_id=None, commit=True):
    """Take the model's answer, as a person's decision.

    Written through the same path a person clicking the dropdown uses, and
    marked manual, because that is what it now is: somebody read it and
    agreed. What the model said is kept beside it, so the origin of the
    choice stays legible.

    The model is asked for a note code, not a statement line - see the
    module docstring - but `unmapped()` offers it exactly the accounts
    that have no statement line either, so accepting a code that settles
    one is also the answer to that. Most codes do: BS-RPP only ever means
    trade_payables. Where a code covers several lines - PL-ADM alone
    covers a dozen - which one is meant is still unsettled, and the
    account stays in the unmapped list for a person to place, the same as
    before this looked.
    """
    from datetime import datetime

    account = db.session.get(TrialBalanceAccount, suggestion.account_id)
    if account is None or not suggestion.code:
        return False
    account.line_code = suggestion.code
    account.line_code_source = "ai-accepted"
    account.mapping_is_manual = True
    account.mapping_source = "ai"
    if not account.standard_key:
        resolved = line_codes.standard_key_for_code(suggestion.code)
        if resolved:
            account.standard_key = resolved
    suggestion.decision = "accepted"
    suggestion.decided_by = user_id
    suggestion.decided_at = datetime.utcnow()
    if commit:
        db.session.commit()
    return True


def reject(suggestion, user_id=None, commit=True):
    """Say no, and keep the no. A rejected suggestion is not re-offered."""
    from datetime import datetime

    suggestion.decision = "rejected"
    suggestion.decided_by = user_id
    suggestion.decided_at = datetime.utcnow()
    if commit:
        db.session.commit()
    return True


def state(financial_year):
    """What the mapping screen needs: the accounts, and anything said about them."""
    said = {row.account_id: row for row in AiMappingSuggestion.query
            .filter_by(financial_year_id=financial_year.id).all()}
    labels = line_codes.code_labels(financial_year) or {}
    rows = []
    for account in unmapped(financial_year):
        suggestion = said.get(account.id)
        rows.append({
            "account": account,
            "name": (account.account_name or "").strip(),
            "side": _side(account),
            "suggestion": suggestion,
            "label": labels.get(suggestion.code) if suggestion
            and suggestion.code else None,
        })
    return rows
