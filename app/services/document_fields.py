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
ENTERED = ("TAX",)


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
    """
    version = _version(financial_year)
    if version is None:
        return []
    fields = []
    for row in version.sheet("Binding fields"):
        if str(row.get("Token") or "").strip().strip(":") != token:
            continue
        fields.append({
            "token": token,
            "field": str(row.get("Field") or "").strip(),
            "binding": f"{token}:{str(row.get('Field') or '').strip()}",
            "meaning": row.get("Meaning") or "",
            "type": (row.get("Type") or "amount").strip(),
            "document": row.get("Source document") or "",
            "how": row.get("Derivation and cross-check") or "",
            "blocking": _blocking(row.get("Blocking")),
            "blocking_note": str(row.get("Blocking") or "").strip(),
        })
    return fields


def documents(financial_year):
    """Every document whose figures can be typed in, with its fields."""
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
        row = described.get(token) or {}
        answers = stored(financial_year, token)
        out.append({
            "token": token,
            "name": row.get("Document") or token,
            "supplies": row.get("What it supplies") or "",
            "notes": row.get("Notes that depend on it") or "",
            "required": str(row.get("Required?") or "").strip(),
            "fields": [dict(field, answer=answers.get(field["field"]))
                       for field in fields],
            "missing": missing(financial_year, token),
        })
    return out


def stored(financial_year, token=None):
    """{field: DocumentFigure} for one token, or {"TOKEN:field": ...} for all."""
    query = DocumentFigure.query.filter_by(financial_year_id=financial_year.id)
    if token:
        return {row.field: row for row in query.filter_by(token=token).all()
                if row.is_answered}
    return {row.binding: row for row in query.all() if row.is_answered}


def value(financial_year, token, field):
    """The entered figure, or None if nobody has answered."""
    row = DocumentFigure.query.filter_by(
        financial_year_id=financial_year.id, token=token, field=field).first()
    if row is None or not row.is_answered:
        return None
    return row


def missing(financial_year, token):
    """Blocking fields on this document that nobody has answered yet."""
    answered = stored(financial_year, token)
    return [field for field in catalogue(financial_year, token)
            if field["blocking"] and field["field"] not in answered]


def is_blocking(financial_year, token, field):
    for known in catalogue(financial_year, token):
        if known["field"] == field:
            return known["blocking"]
    # A binding the library does not define. Held rather than dropped: an
    # unknown token is a fault in the library or the import, and silently
    # leaving its row out would hide it.
    return True


def save(financial_year, token, field, *, amount=None, text=None,
         found_at=None, clear=False):
    """Record one figure from a document. Returns the row, or None if cleared.

    Clearing removes the answer, which puts the row back to Incomplete. It
    does not write a zero: a preparer who deletes what they typed has gone
    back to not having answered, and the accounts must say so.
    """
    from flask_login import current_user

    from .audit import record

    row = DocumentFigure.query.filter_by(
        financial_year_id=financial_year.id, token=token, field=field).first()
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
                             token=token, field=field)
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
           after={"binding": row.binding, "amount": str(row.amount),
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
