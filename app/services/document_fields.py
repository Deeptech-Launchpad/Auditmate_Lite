"""Figures that come from a document AuditMate does not read.

The trial balance is loaded, mapped and totalled by the app. A handful of
figures are not in it at all - the tax computation, the fixed asset
register, the aged receivables listing, last year's signed accounts - and
the notes library names each one as a binding token and a field, with the
document it comes from and whether the note can be issued without it.

Two ways such a figure can arrive: parsed from an export, or typed in by
the preparer. The tax computation is typed, on the firm's own
instruction - "five figures for a client this size" - and a PDF that
arrives in a different layout every year is not worth a parser.

The rule this module keeps, and the reason it exists rather than the
values being stored loose:

    NOT ENTERED IS NOT NIL. A field nobody has answered holds its row
    "Incomplete". A field answered with zero prints a dash. The two are
    different statements about the company and the engine must never
    turn the first into the second - which is exactly what a default of
    0 in a form would do.

BLOCKING. The library marks each field blocking or not, and the difference
is real. A blocking field that is unanswered holds the note incomplete,
because the note cannot be issued without it. A non-blocking one - the
unutilised losses carried forward, the unabsorbed capital allowances -
leaves its row out entirely: a company with no tax losses is not missing a
disclosure, and printing "Incomplete" against it would stop a set of
accounts that is finished.

Nothing here calls the AI.
"""
import logging

from ..extensions import db
from ..models import DocumentFigure, NoteLibraryVersion

log = logging.getLogger(__name__)

# Tokens a preparer may type in. Everything else in the library's Source
# documents sheet is meant to be read from an export, and typing those by
# hand would be transcribing hundreds of rows, not five figures.
#
# PRIORFS is here because of the firm's third decision: last year's SIGNED
# accounts are now a primary source, and a signed set arrives as a PDF that
# a parser may or may not read correctly. Where it does, the figures come
# through services/prior_year.py; where it does not, a person reads the
# statement and types the figure, and what they typed outranks the parser.
ENTERED = ("TAX", "PRIORFS")

# Fields whose name means a different figure in each note that uses it.
# PRIORFS:cost_open_py is the opening cost of plant and equipment in one
# note, of investment property in another and of intangibles in a third,
# and nothing but the note it sits in tells them apart.
SCOPED = ("PRIORFS", "FAR")

# The library spells one token two ways. Its Binding fields sheet and every
# note bind PRIORFS:, while its Source documents sheet describes the same
# document under PRIOR:. Mapped here rather than guessed at each use, and
# raised with the firm rather than silently accommodated.
DOCUMENT_ALIASES = {"PRIORFS": "PRIOR"}


def scope_of(token, scope):
    """The note a figure belongs to, or "" where the token does not care.

    Callers hand this the table a row sits in, because a row always knows
    that. Only a token whose field names repeat across notes uses it - the
    tax computation has one "current tax charge" however many tables print
    it, and looking that up under a table id would find nothing.

    One rule in one place: every read and every write goes through here, so
    a figure cannot be saved under one scope and looked up under another.
    """
    return (scope or "") if token in SCOPED else ""


def _version(financial_year):
    if getattr(financial_year, "library_version_id", None):
        version = NoteLibraryVersion.query.get(financial_year.library_version_id)
        if version is not None:
            return version
    return NoteLibraryVersion.query.filter_by(status="active").first()


def _blocking(raw):
    """The library's Blocking column. "Yes where deferred tax exists" is a Yes.

    A conditional yes is treated as a yes on purpose: the condition is one
    a person judges, and the safe reading of "blocking where it applies" is
    to ask rather than to assume it does not apply.
    """
    return str(raw or "").strip().lower().startswith("yes")


def catalogue(financial_year, token):
    """The library's own definition of every field on one document.

    Read from the version this engagement is pinned to, not the newest, so
    a period keeps reporting under the library in force for it.

    The wildcard row - PRIORFS's "<any printed figure>" - is left out. It
    is the library stating a principle, that any figure in last year's
    signed accounts can be taken from it, not a field anybody types into.
    """
    version = _version(financial_year)
    if version is None:
        return []
    fields = []
    for row in version.sheet("Binding fields"):
        if str(row.get("Token") or "").strip().strip(":") != token:
            continue
        name = str(row.get("Field") or "").strip()
        if not name or name.startswith("<"):
            continue
        fields.append({
            "token": token,
            "field": name,
            "binding": f"{token}:{name}",
            "meaning": row.get("Meaning") or "",
            "type": (row.get("Type") or "amount").strip(),
            "document": row.get("Source document") or "",
            "how": row.get("Derivation and cross-check") or "",
            "blocking": _blocking(row.get("Blocking")),
            "blocking_note": str(row.get("Blocking") or "").strip(),
        })
    return fields


def scopes_for(financial_year, token):
    """Which notes use this token's fields, and under what heading.

    Read from the library's own tables rather than listed here, so a
    version that adds a fourth asset note needs no code change. Only
    tables this engagement's library actually binds to the token appear -
    a company with no intangibles still sees the note listed, and its
    fields stay empty, which is the honest state until somebody says
    otherwise.
    """
    from ..models import NoteLibraryNote

    version = _version(financial_year)
    if version is None:
        return []
    found = []
    for note in NoteLibraryNote.query.filter_by(
            library_version_id=version.id).all():
        for piece in note.pieces or []:
            table_id = piece.get("table_id")
            if not table_id:
                continue
            uses = {str(row.get("binding") or "").split(":")[0]
                    for row in piece.get("rows") or []}
            if token in uses:
                found.append({"scope": table_id,
                              "note": note.heading or note.library_code,
                              "note_code": note.library_code,
                              "fields": [
                                  str(row.get("binding") or "").split(":", 1)[-1]
                                  for row in piece.get("rows") or []
                                  if str(row.get("binding") or "").startswith(
                                      token + ":")]})
    return found


def _note_applies(financial_year, note_code):
    """Whether this company has the thing the note is about.

    A company with no investment property is not missing last year's
    investment property movement, and asking for twelve figures it will
    never print would bury the ten that matter. Decided the same way the
    note itself decides whether to print: the lines it is about, and
    whether either year carries a balance on them.
    """
    if not note_code:
        return True
    from . import bindings, conditions

    figures = bindings.figures_for(financial_year)
    subjects = conditions.subject_codes(figures, note_code)
    if not subjects:
        return True                     # nothing to judge on; ask, not assume
    return any(conditions.carries_balance(figures, code) for code in subjects)


def _signed_set_group(financial_year):
    """Last year's figure for every line the accounts print, as signed.

    One box per statement line, because that is the level a set of accounts
    reports at and the level the comparative column prints at. The current
    year's figure is shown beside each as context - it is what a preparer
    reads down the page against, and it is the only way to find the line
    they are looking for without knowing its internal key.

    None of these is blocking. A comparative already has a source - our own
    previous engagement, or the prior-year column of the trial balance -
    and this is here to override that source with what was actually filed,
    not to hold the accounts until somebody retypes last year's balance
    sheet. The Preparer checks page (PC-03) is where the comparison is put
    in front of a person.

    So each line shows what the comparative column prints TODAY, and where
    that came from. Nobody should have to type eighty figures: they read
    down the two columns and type only where the signed set differs, which
    on a normal engagement is a handful of lines or none at all.
    """
    from ..models import FinancialStatement
    from . import prior_year

    answers = stored(financial_year, "PRIORFS")
    _figures, source = prior_year.balances(financial_year)
    where = prior_year.SOURCE_LABELS.get(source, "no source yet")

    fields, seen = [], set()
    for statement in FinancialStatement.query.filter_by(
            financial_year_id=financial_year.id).all():
        if statement.statement_type == "trial_balance":
            continue
        for line in statement.lines:
            if line.line_key in seen or line.is_detail:
                continue
            seen.add(line.line_key)
            prints = line.amount_previous
            fields.append({
                "token": "PRIORFS",
                "field": line.line_key,
                "binding": f"PRIORFS:{line.line_key}",
                "scope": "",
                "meaning": line.effective_label,
                "type": "amount",
                "document": "Prior year signed financial statements",
                "statement": statement.type_label,
                "prints_now": prints,
                # A plain character, not an HTML entity: this string is
                # built from a label a preparer can edit, so it must stay
                # escapable all the way to the page.
                "how": (f"{statement.type_label} · prints "
                        f"{prints:,.0f} from {where}"
                        if prints is not None
                        else f"{statement.type_label} · no comparative "
                             f"from {where}"),
                "blocking": False,
                "blocking_note": "",
                "answer": answers.get(line.line_key),
            })
    return {"scope": "",
            "heading": "As last year's accounts were signed",
            "hint": (f"The comparative column currently comes from {where}. "
                     f"Type a figure only where the signed set differs."),
            "fields": fields, "missing": []}


def documents(financial_year):
    """Every document whose figures can be typed in, with its fields.

    A scoped document comes back as one group per note that uses it,
    because the same twelve field names describe three different asset
    notes and a single flat list of them would be unanswerable.
    """
    version = _version(financial_year)
    if version is None:
        return []
    described = {}
    for row in version.sheet("Source documents"):
        token = str(row.get("Token") or "").strip().strip(":")
        described[token] = row

    out = []
    for token in ENTERED:
        fields = catalogue(financial_year, token)
        if not fields:
            continue
        row = described.get(token) or described.get(
            DOCUMENT_ALIASES.get(token, "")) or {}
        document = {
            "token": token,
            "name": row.get("Document") or token,
            "supplies": row.get("What it supplies") or "",
            "notes": row.get("Notes that depend on it") or "",
            "required": str(row.get("Required?") or "").strip(),
            "groups": [],
        }

        if token == "PRIORFS":
            # The library's wildcard: "any figure printed in the prior
            # year's signed financial statements". Reached by naming the
            # line it sits on, so a preparer can correct or supply one
            # comparative without touching the books. What is typed here
            # outranks every trial balance, including our own previous
            # engagement - see services/prior_year.SOURCE_ORDER.
            document["groups"].append(_signed_set_group(financial_year))

        if token in SCOPED:
            for entry in scopes_for(financial_year, token):
                if not _note_applies(financial_year, entry["note_code"]):
                    continue
                wanted = [field for field in fields
                          if field["field"] in set(entry["fields"])]
                answers = stored(financial_year, token, entry["scope"])
                document["groups"].append({
                    "scope": entry["scope"],
                    "heading": entry["note"],
                    "fields": [dict(field, scope=entry["scope"],
                                    answer=answers.get(field["field"]))
                               for field in wanted],
                    "missing": missing(financial_year, token, entry["scope"]),
                })
        else:
            answers = stored(financial_year, token)
            document["groups"].append({
                "scope": "",
                "heading": None,
                "fields": [dict(field, scope="",
                                answer=answers.get(field["field"]))
                           for field in fields],
                "missing": missing(financial_year, token),
            })

        document["missing"] = [field for group in document["groups"]
                               for field in group["missing"]]
        out.append(document)
    return out


def stored(financial_year, token=None, scope=""):
    """{field: DocumentFigure} for one token in one note.

    With no token, everything entered for the engagement keyed by its
    binding and note, so a reviewer's list can show where each came from.
    """
    query = DocumentFigure.query.filter_by(financial_year_id=financial_year.id)
    if token:
        return {row.field: row
                for row in query.filter_by(
                    token=token, scope=scope_of(token, scope)).all()
                if row.is_answered}
    return {row.where: row for row in query.all() if row.is_answered}


def value(financial_year, token, field, scope=""):
    """The entered figure, or None if nobody has answered."""
    row = DocumentFigure.query.filter_by(
        financial_year_id=financial_year.id, token=token, field=field,
        scope=scope_of(token, scope)).first()
    if row is None or not row.is_answered:
        return None
    return row


def missing(financial_year, token, scope=""):
    """Blocking fields on this document that nobody has answered yet."""
    scope = scope_of(token, scope)
    answered = stored(financial_year, token, scope)
    wanted = catalogue(financial_year, token)
    if scope:
        # Only the fields this note actually binds. The plant and
        # equipment note carries impairment rows; the intangibles note in
        # this library does not, and holding it for a field it never
        # prints would be asking for a figure with nowhere to go.
        allowed = {name for entry in scopes_for(financial_year, token)
                   if entry["scope"] == scope for name in entry["fields"]}
        wanted = [field for field in wanted if field["field"] in allowed]
    return [field for field in wanted
            if field["blocking"] and field["field"] not in answered]


def is_blocking(financial_year, token, field, scope=""):
    for known in catalogue(financial_year, token):
        if known["field"] == field:
            return known["blocking"]
    # A binding the library does not define. Held rather than dropped: an
    # unknown token is a fault in the library or the import, and silently
    # leaving its row out would hide it.
    return True


def save(financial_year, token, field, *, scope="", amount=None, text=None,
         found_at=None, clear=False):
    """Record one figure from a document. Returns the row, or None if cleared.

    Clearing removes the answer, which puts the row back to Incomplete. It
    does not write a zero: a preparer who deletes what they typed has gone
    back to not having answered, and the accounts must say so.
    """
    from flask_login import current_user

    from .audit import record

    scope = scope_of(token, scope)
    row = DocumentFigure.query.filter_by(
        financial_year_id=financial_year.id, token=token, field=field,
        scope=scope).first()
    before = None if row is None else {"amount": str(row.amount),
                                       "text": row.text}

    if clear or (amount is None and not (text or "").strip()):
        if row is None:
            return None
        record("document_figure", row.id, "clear", before=before)
        db.session.delete(row)
        db.session.commit()
        return None

    if row is None:
        row = DocumentFigure(financial_year_id=financial_year.id,
                             token=token, field=field, scope=scope)
        db.session.add(row)
    row.amount = amount
    row.text = (text or "").strip() or None
    row.found_at = (found_at or "").strip() or None
    try:
        row.entered_by = current_user.id if current_user.is_authenticated else None
    except Exception:                                   # outside a request
        pass

    db.session.flush()
    record("document_figure", row.id, "enter", before=before,
           after={"binding": row.where, "amount": str(row.amount),
                  "found_at": row.found_at})
    db.session.commit()
    return row


def summary(financial_year):
    """One line per enterable document, for a screen that lists engagements."""
    out = []
    for document in documents(financial_year):
        answered = sum(1 for field in document["fields"] if field["answer"])
        out.append({
            "token": document["token"],
            "name": document["name"],
            "answered": answered,
            "total": len(document["fields"]),
            "missing": len(document["missing"]),
        })
    return out
