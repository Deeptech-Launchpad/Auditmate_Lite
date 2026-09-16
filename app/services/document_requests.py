"""What this engagement still needs, and who has to produce it.

The library's *Source documents* sheet lists fifteen tokens, and for each
one the document behind it, what it supplies, whether it is mandatory,
and the notes that depend on it. That is already a request list; it has
just never been shown as one. This assembles it per engagement.

Three things are added to the sheet's own columns:

Relevance. A company with no borrowings does not need loan agreements,
and putting them on the list teaches the preparer to skim it. A document
is asked for only where a note that depends on it applies - decided by
`document_fields._note_applies`, the same test the note itself uses.

State. Where the engagement records whether something arrived, it is
said: the four documents whose figures are typed in report how many
fields are still blank, and the ones that arrive as an upload report
whether a file of that category is filed. For the rest AuditMate holds
no record of receipt, and the list says exactly that rather than
implying the thing is outstanding when nobody knows.

An owner. The sheet has no Who column - the *Files to obtain* sheet has
one, but that is the firm's list of sample exports owed to us, not a
per-engagement list. So OWED_BY below is ours, not the library's, and
it is on the list of things to put to the firm.
"""
import logging

from ..models import DISCLOSURE_SETTING_KEYS, Document
from . import document_fields

log = logging.getLogger(__name__)

# Whose desk each document comes off. Ours, not the library's - see the
# module docstring. Kept as one table so that when the firm rules on it,
# there is a single place to correct.
OWED_BY = {
    "TB": "client",
    "GL": "client",
    "PRIOR": "client",
    "FAR": "client",
    "AGED": "client",
    "REG": "client",
    "LOAN": "client",
    "BANK": "client",
    "CLIENT": "client",
    # A tax computation is usually the tax agent's work product, and asking
    # the bookkeeper for it wastes a round trip.
    "TAX": "tax_agent",
    "FIRM": "firm",
    # Neither of these is a file anyone can send. They are answers, and the
    # person who answers them is the preparer.
    "MANUAL": "preparer",
    "MEMO": "preparer",
}

OWNERS = [
    ("client", "The client"),
    ("tax_agent", "The client's tax agent"),
    ("preparer", "The preparer"),
    ("firm", "Your firm"),
]

# Where a document arrives as an upload, the category it is filed under.
# Only the current-year categories: last year's twin of a listing is not
# what this year's note needs.
UPLOADED_AS = {
    "TB": ("trial_balance",),
    "GL": ("general_ledger",),
    "PRIOR": ("signed_accounts",),
    "FAR": ("fixed_asset_register",),
    "AGED": ("receivables",),
    "TAX": ("tax_document",),
    "BANK": ("bank_statement",),
}

# The sheet's own way of saying a row is not a document at all. Both rows
# that carry it - STATIC, and the CALC leftover from before the engine
# stopped computing - drop out here rather than by name.
NOT_A_DOCUMENT = "not a document"

IN_HAND = "in hand"
PART = "partly in hand"
OUTSTANDING = "outstanding"
UNTRACKED = "not recorded"


def _token_of(row):
    return str(row.get("Token") or "").strip().strip(":")


def _note_codes(row):
    """The note codes a Source documents row depends on, as a list."""
    raw = str(row.get("Notes that depend on it") or "")
    return [part.strip() for part in raw.replace(";", ",").split(",")
            if part.strip().upper().startswith("N")]


def _relevant(financial_year, codes):
    """Which of those notes this company actually has the thing for.

    A row naming no note codes - "Throughout", "All breakup notes" - is
    not narrowed, because there is nothing to narrow it by. Asked for.
    """
    if not codes:
        return None
    out = []
    for code in codes:
        try:
            if document_fields._note_applies(financial_year, code):
                out.append(code)
        except Exception:               # pragma: no cover - never block a list
            log.exception("Relevance of note %s could not be judged", code)
            out.append(code)
    return out


def _entered_state(financial_year, token):
    """How many of a typed document's blocking fields are still blank."""
    if not token:
        return None
    scopes = [""]
    if token in document_fields.SCOPED:
        scopes = [entry["scope"]
                  for entry in document_fields.scopes_for(financial_year, token)
                  if document_fields._note_applies(financial_year,
                                                   entry["note_code"])]
    wanted = answered = 0
    for scope in scopes:
        blocking = [f for f in document_fields.catalogue(financial_year, token)
                    if f["blocking"]]
        held = document_fields.stored(financial_year, token, scope)
        for field in blocking:
            wanted += 1
            answer = held.get(field["field"])
            if answer is not None and answer.is_answered:
                answered += 1
    if not wanted:
        return None
    state = IN_HAND if answered == wanted else (
        OUTSTANDING if not answered else PART)
    return {"state": state, "have": answered, "total": wanted,
            "measured": "figures entered"}


def _uploaded_state(financial_year, token):
    """Whether a file of the matching category is filed for this year."""
    categories = UPLOADED_AS.get(token)
    if not categories:
        return None
    filed = (Document.query
             .filter(Document.financial_year_id == financial_year.id)
             .filter(Document.category.in_(categories)).count())
    return {"state": IN_HAND if filed else OUTSTANDING,
            "have": filed, "total": None, "measured": "files uploaded"}


# The CLIENT row names five things: "Name, registration number, registered
# office, principal activities, directors". Each is a field on the customer
# record, so whether it is in hand is a fact rather than a guess.
CLIENT_RECORD = (
    ("name", lambda c: c.name),
    ("registration number", lambda c: c.uen),
    ("registered office", lambda c: c.address_line1 and c.postal_code),
    ("principal activities", lambda c: c.principal_activities),
    ("directors", lambda c: c.directors),
)


def _recorded_state(financial_year, token):
    """Documents that are not files and not typed figures, but records.

    The client master record and the firm's standing wording both live in
    AuditMate already. Reporting them as "not recorded" would send a
    preparer chasing something they are sitting on.
    """
    if token == "CLIENT":
        customer = financial_year.customer
        held = sum(1 for _label, get in CLIENT_RECORD
                   if str(get(customer) or "").strip())
        return {"state": IN_HAND if held == len(CLIENT_RECORD) else (
                    OUTSTANDING if not held else PART),
                "have": held, "total": len(CLIENT_RECORD),
                "measured": "fields on the client record",
                "outstanding_parts": [label for label, get in CLIENT_RECORD
                                      if not str(get(customer) or "").strip()]}
    if token == "FIRM":
        from . import disclosure_settings

        unset = disclosure_settings.unset_keys(financial_year.customer)
        total = len(DISCLOSURE_SETTING_KEYS)
        held = total - len(unset)
        return {"state": IN_HAND if not unset else (
                    OUTSTANDING if not held else PART),
                "have": held, "total": total,
                "measured": "settings answered",
                "outstanding_parts": sorted(unset)}
    return None


def requests(financial_year):
    """Every source document this engagement needs, most urgent first."""
    version = document_fields._version(financial_year)
    if version is None:
        return []

    out = []
    for row in version.sheet("Source documents"):
        required = str(row.get("Required?") or "").strip()
        if required.lower() == NOT_A_DOCUMENT:
            continue
        token = _token_of(row)
        codes = _note_codes(row)
        relevant = _relevant(financial_year, codes)
        if relevant == []:              # named notes, none of which apply
            continue

        # A typed document knows precisely what is missing, so it wins over
        # the cruder "is a file filed" test where both could answer.
        state = (_entered_state(financial_year,
                                _entered_token(token))
                 or _recorded_state(financial_year, token)
                 or _uploaded_state(financial_year, token)
                 or {"state": UNTRACKED, "have": None, "total": None,
                     "measured": "not recorded in AuditMate"})

        owner = OWED_BY.get(token, "preparer")
        out.append({
            "token": token,
            "name": row.get("Document") or token,
            "supplies": row.get("What it supplies") or "",
            "required": required,
            "mandatory": required.lower().startswith("mandatory"),
            "notes": relevant if relevant is not None else codes,
            "all_notes": codes,
            "narrowed": relevant is not None and len(relevant) < len(codes),
            "owner": owner,
            "owner_label": dict(OWNERS).get(owner, owner),
            "outstanding_parts": [],
            **state,
        })

    out.sort(key=lambda r: (_RANK.get(r["state"], 9),
                            0 if r["mandatory"] else 1, r["name"]))
    return out


# Outstanding first: the list exists to be acted on, and what is already
# in hand is only there so the preparer can see it was not forgotten.
_RANK = {OUTSTANDING: 0, PART: 1, UNTRACKED: 2, IN_HAND: 3}


def _entered_token(token):
    """The ENTERED spelling of a Source documents token, where there is one.

    The sheet writes PRIOR:, the binding fields and every note row write
    PRIORFS:. One of the two is a typo in the workbook; until the firm
    says which, both are understood.
    """
    for entered in document_fields.ENTERED:
        if entered == token or document_fields.DOCUMENT_ALIASES.get(
                entered) == token:
            return entered
    return None


def by_owner(financial_year):
    """The same list, grouped by who has to produce each thing."""
    rows = requests(financial_year)
    groups = []
    for key, label in OWNERS:
        mine = [row for row in rows if row["owner"] == key]
        if mine:
            groups.append({"key": key, "label": label, "rows": mine,
                           "outstanding": sum(1 for r in mine
                                              if r["state"] != IN_HAND)})
    return groups


def summary(financial_year):
    """One line for the top of a page: how much of the list is settled."""
    rows = requests(financial_year)
    return {
        "total": len(rows),
        "in_hand": sum(1 for r in rows if r["state"] == IN_HAND),
        "outstanding": sum(1 for r in rows if r["state"] == OUTSTANDING),
        "partly": sum(1 for r in rows if r["state"] == PART),
        "untracked": sum(1 for r in rows if r["state"] == UNTRACKED),
    }
