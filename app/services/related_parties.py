"""Who this company's related parties are, and what in the books is theirs.

The notes library's Known issues sheet opens with this one, KI-01:

    Every entry in the test client's director loan account is Spend Money
    or Receive Money, so no parsing rule recovers the counterparty, and
    the director appears under four spellings. Needs a related party name
    list taken from the preparer once per engagement, with the matches
    shown back for confirmation. A design decision on how the matching is
    presented, not a parser fix.

So this module does not try to work out who is related. It cannot: being
a director's wife is not a property of a ledger entry, and the books
record it nowhere. What it does is hold the list a person gives it, put
the things in the books that look like they belong to someone on that
list in front of that person, and record what they decide.

Three rules hold it together:

  A SUGGESTION IS NEVER A DECISION. Nothing here writes a match on its
  own. A name similar enough to suggest is exactly how an unrelated
  supplier ends up disclosed as a director's company, and the note that
  results is a false statement about people, not a wrong subtotal.

  A REJECTION IS KEPT. Otherwise the same wrong suggestion returns every
  time the page is opened, and the preparer who dismissed it has no way
  to show that they did.

  UNDECIDED HOLDS THE NOTE. A candidate nobody has ruled on leaves the
  related party notes incomplete, naming it. Treating silence as "not
  related" would quietly drop a disclosure, which is the failure the
  whole no-arithmetic design exists to avoid.

Nothing here calls the AI. Matching is done on spellings a person typed.
"""
import logging
import re

from ..extensions import db
from ..models import (RELATED_PARTY_KINDS, RelatedParty, RelatedPartyMatch,
                      TrialBalanceAccount)

log = logging.getLogger(__name__)

KINDS = RELATED_PARTY_KINDS

# Words that make a caption look related without naming anybody. An account
# called "Loan from Director" is a candidate precisely because it says so,
# and it still has to be put in front of a person: which director, and is
# the loan account really theirs.
SIGNAL_WORDS = ("director", "shareholder", "related part", "related compan",
                "holding compan", "subsidiar", "associate", "key management",
                "intercompany", "inter-company", "due from related",
                "due to related")

# Line codes the library reserves for related party balances. An account
# mapped to one of these is a candidate whatever it is called.
RELATED_CODES = ("BS-RPR", "BS-RPP")

_PUNCT = re.compile(r"[^a-z0-9 ]+")

# The shortest spelling worth suggesting on. A preparer who types "A" as
# one of a director's initials would otherwise see every account in the
# trial balance suggested against them, which trains people to click
# through the confirmations without reading - the exact failure the
# shown-back design exists to prevent.
SHORTEST_SPELLING = 3


def normalise(text):
    """Lowercase, punctuation out, whitespace collapsed.

    The same fold the library's own Matching rules apply to note headings
    (rules 2 and 6). "Mr. Tan Ah-Kow" and "TAN AH KOW" become one string;
    they are still only a suggestion.
    """
    return " ".join(_PUNCT.sub(" ", str(text or "").lower()).split())


# --------------------------------------------------------------------------
# The register
# --------------------------------------------------------------------------

def register(financial_year):
    return (RelatedParty.query
            .filter_by(financial_year_id=financial_year.id)
            .order_by(RelatedParty.name).all())


def directors_of(customer):
    """Names on the customer's own Directors field, one per line.

    The same field the cover page and the Directors' Statement print
    from. Kept separate from the register on purpose - a director is
    not automatically a related party for THIS engagement's figures,
    only a very likely candidate to be one - but there is no reason to
    make a preparer type the same name twice.
    """
    raw = (customer.directors or "").strip()
    if not raw:
        return []
    seen, names = set(), []
    for line in raw.splitlines():
        name = " ".join(line.strip().split())
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


def missing_directors(financial_year):
    """Directors on the customer record not yet on this engagement's list.

    What the "Add the client's directors" button on the Related parties
    page offers - never run silently, so a page load never writes to the
    database. A director already on the register (added by hand, or
    carried from last year, however spelled) does not appear twice.
    """
    on_record = {p.name.strip().lower()
                for p in register(financial_year)}
    return [name for name in directors_of(financial_year.customer)
            if name.lower() not in on_record]


def add_directors(financial_year, names):
    """Add several directors at once, exactly as add() would one at a time.

    No spellings are guessed - the customer record gives a name, not the
    handful of ways it appears across a trial balance, and inventing
    those would be worse than leaving the box empty for someone to fill.
    """
    added = [add(financial_year, name, kind="director") for name in names]
    return added


def add(financial_year, name, kind="director", spellings=None, note=None):
    from flask_login import current_user

    from .audit import record

    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("A related party needs a name.")

    party = RelatedParty(
        financial_year_id=financial_year.id, name=name,
        kind=kind if kind in dict(KINDS) else "other",
        spellings=_clean_spellings(spellings), note=(note or "").strip() or None)
    try:
        party.created_by = current_user.id if current_user.is_authenticated else None
    except Exception:                                      # outside a request
        pass
    db.session.add(party)
    db.session.flush()
    record("related_party", party.id, "add",
           after={"name": party.name, "kind": party.kind})
    db.session.commit()
    return party


def update(party, *, name=None, kind=None, spellings=None, note=None):
    from .audit import record

    before = {"name": party.name, "kind": party.kind,
              "spellings": list(party.spellings or [])}
    if name is not None:
        cleaned = " ".join(str(name).split())
        if cleaned:
            party.name = cleaned
    if kind is not None and kind in dict(KINDS):
        party.kind = kind
    if spellings is not None:
        party.spellings = _clean_spellings(spellings)
    if note is not None:
        party.note = (note or "").strip() or None
    record("related_party", party.id, "update", before=before,
           after={"name": party.name, "kind": party.kind,
                  "spellings": list(party.spellings or [])})
    db.session.commit()
    return party


def remove(party):
    """Take a party off the list. Its decisions go with it.

    Deliberate: a confirmed match points at a party, and a match pointing
    at nobody is not a record of anything. The audit trail keeps what was
    removed.

    A later year carried from this one keeps its own entry and simply
    loses the link. Removing a director from FY2023 must not reach into
    FY2024 and remove them there - that year has been signed, or is being
    prepared by somebody else, and its register is its own.
    """
    from .audit import record

    inherited = RelatedParty.query.filter_by(carried_from_id=party.id).all()
    for later in inherited:
        later.carried_from_id = None

    record("related_party", party.id, "remove",
           before={"name": party.name, "matches": len(party.matches),
                   "inherited_by": len(inherited)})
    db.session.delete(party)
    db.session.commit()


def _clean_spellings(raw):
    """A list of names from a list, or one per line of a textarea."""
    if raw is None:
        return []
    items = raw.splitlines() if isinstance(raw, str) else list(raw)
    seen, out = set(), []
    for item in items:
        name = " ".join(str(item or "").split())
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out


# --------------------------------------------------------------------------
# Candidates and suggestions
# --------------------------------------------------------------------------

def candidates(financial_year):
    """Everything in the books that might belong to a related party.

    Today that is trial balance accounts; when a general ledger is loaded
    its entries join the same list, which is why a match is addressed by
    subject type and id rather than by an account foreign key.

    An account is a candidate when it is mapped to one of the library's
    related party line codes, when its caption uses a word that signals a
    related party, or when its caption resembles a name on the register.
    The first two find things the register has not heard of - which is the
    point: a preparer who forgot to list the landlord company still gets
    the account put in front of them.
    """
    parties = register(financial_year)
    named = [(party, normalise(spelling))
             for party in parties for spelling in party.all_names
             if len(normalise(spelling)) >= SHORTEST_SPELLING]
    decided = {(m.subject_type, m.subject_id): m
               for m in RelatedPartyMatch.query.filter_by(
                   financial_year_id=financial_year.id).all()}

    found = []
    for account in TrialBalanceAccount.query.filter_by(
            financial_year_id=financial_year.id).all():
        caption = normalise(account.account_name)
        if not caption:
            continue

        why, suggested, matched_on = None, None, None
        if (account.line_code or "") in RELATED_CODES:
            why = "mapped to a related party line"
        for party, spelling in named:
            if spelling and (spelling in caption or caption in spelling):
                suggested, matched_on = party, spelling
                why = why or f"the caption contains “{spelling}”"
                break
        if why is None:
            for word in SIGNAL_WORDS:
                if word in caption:
                    why = f"the caption says “{word}”"
                    break
        if why is None:
            continue

        decision = decided.get(("tb_account", account.id))
        found.append({
            "subject_type": "tb_account",
            "subject_id": account.id,
            "label": account.account_name,
            "code": account.account_code,
            "amount": _net(account),
            "line_code": account.line_code,
            "why": why,
            "suggested": suggested,
            "matched_on": matched_on,
            "decision": decision,
            "settled": decision is not None,
            "renamed": bool(decision is not None
                            and decision.subject_label
                            and decision.subject_label != account.account_name),
        })

    found.sort(key=lambda item: (item["settled"], -abs(item["amount"] or 0)))
    return found


def _net(account):
    from decimal import Decimal

    return (Decimal(str(account.debit or 0))
            - Decimal(str(account.credit or 0)))


def undecided(financial_year):
    """Candidates nobody has ruled on. These hold the related party notes."""
    return [item for item in candidates(financial_year) if not item["settled"]]


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------

def decide(financial_year, subject_type, subject_id, *, party_id=None,
           subject_label=None, matched_on=None):
    """Record that a person ruled on one candidate.

    `party_id` names the related party it belongs to; None means the
    person looked and said it is not a related party at all. Both are
    decisions and both are kept.
    """
    from flask_login import current_user

    from .audit import record

    match = RelatedPartyMatch.query.filter_by(
        financial_year_id=financial_year.id, subject_type=subject_type,
        subject_id=subject_id).first()
    if match is None:
        match = RelatedPartyMatch(financial_year_id=financial_year.id,
                                  subject_type=subject_type,
                                  subject_id=subject_id)
        db.session.add(match)

    before = {"party": match.party_id, "decision": match.decision}
    match.party_id = party_id
    match.decision = "confirmed" if party_id else "rejected"
    match.subject_label = subject_label or match.subject_label
    match.matched_on = matched_on or match.matched_on
    try:
        match.decided_by = current_user.id if current_user.is_authenticated else None
    except Exception:
        pass

    db.session.flush()
    record("related_party_match", match.id, "decide", before=before,
           after={"party": match.party_id, "decision": match.decision,
                  "subject": f"{subject_type}:{subject_id}"})
    db.session.commit()
    return match


def unsettle(financial_year, subject_type, subject_id):
    """Undo a decision, putting the candidate back in front of a person."""
    from .audit import record

    match = RelatedPartyMatch.query.filter_by(
        financial_year_id=financial_year.id, subject_type=subject_type,
        subject_id=subject_id).first()
    if match is None:
        return
    record("related_party_match", match.id, "unsettle",
           before={"party": match.party_id, "decision": match.decision})
    db.session.delete(match)
    db.session.commit()


# --------------------------------------------------------------------------
# What the notes need
# --------------------------------------------------------------------------

def accounts_of(financial_year, party=None):
    """Trial balance account ids confirmed as a related party's.

    With no party, every account confirmed as related to anybody.
    """
    query = RelatedPartyMatch.query.filter_by(
        financial_year_id=financial_year.id, subject_type="tb_account",
        decision="confirmed")
    if party is not None:
        query = query.filter_by(party_id=party.id)
    return [match.subject_id for match in query.all() if match.party_id]


def parties_in_play(financial_year):
    """Parties with at least one confirmed thing in the books.

    What the note can actually name. A party on the register with nothing
    confirmed against it may still be disclosable - relatedness does not
    require a transaction - but it is not something this engagement's
    figures can point at, so the note says nothing about it by itself.
    """
    confirmed = {match.party_id for match in RelatedPartyMatch.query.filter_by(
        financial_year_id=financial_year.id, decision="confirmed").all()
        if match.party_id}
    return [party for party in register(financial_year)
            if party.id in confirmed]


def state(financial_year):
    """A one-line summary for a screen that lists several engagements."""
    found = candidates(financial_year)
    open_items = [item for item in found if not item["settled"]]
    return {
        "parties": len(register(financial_year)),
        "candidates": len(found),
        "undecided": len(open_items),
        "confirmed": sum(1 for item in found
                         if item["decision"] is not None
                         and item["decision"].is_related),
        "rejected": sum(1 for item in found
                        if item["decision"] is not None
                        and not item["decision"].is_related),
    }


def holds(financial_year):
    """Why the related party notes cannot be issued yet, in words.

    KI-01 says this blocks the related party notes, and it does - but on
    what is genuinely unsettled, never on the register being empty in
    general. A company with no related parties is a real answer; what is
    not an answer is an account that says "Loan from Director" and nobody
    having said whose.
    """
    reasons = []
    # One pass. candidates() reads every account in the trial balance, and
    # this runs for each related party note on every render.
    for item in candidates(financial_year):
        if not item["settled"]:
            reasons.append(
                f"Nobody has said whether “{item['label']}” is a related "
                f"party balance ({item['why']})")
        elif item["renamed"]:
            reasons.append(
                f"“{item['decision'].subject_label}” was decided on "
                f"and is now called “{item['label']}” - check it is "
                f"still the same account")
    return reasons


# --------------------------------------------------------------------------
# Roll-forward
# --------------------------------------------------------------------------

def carry_forward(financial_year):
    """Bring last year's register across. A director does not change.

    Names only, never the matches: the accounts are this year's, and a
    decision about last year's account number is not a decision about
    this year's. Idempotent, and it never touches a party this year's
    preparer has already entered.
    """
    previous = getattr(financial_year, "previous_year", None)
    if previous is None or register(financial_year):
        return 0

    brought = 0
    for old in register(previous):
        db.session.add(RelatedParty(
            financial_year_id=financial_year.id, name=old.name,
            kind=old.kind, spellings=list(old.spellings or []),
            note=old.note, carried_from_id=old.id,
            created_by=old.created_by))
        brought += 1
    if brought:
        db.session.commit()
        log.info("FY %s: carried %d related part(y/ies) forward",
                 financial_year.id, brought)
    return brought
