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
ENTERED = ("TAX", "PRIORFS", "FAR", "AGED")

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

# How a table says it is presented one column per class. The library
# writes it into the table's own column labels - "By class of asset",
# "By class of property", "By class of underlying asset" - and that is
# the only place that knows. The same field is per class in the movement
# table and a single figure in the note above it: FAR:rou_additions is
# one column in the right-of-use additions table and one figure per class
# in the depreciation table, and nothing but the table tells them apart.
BY_CLASS_COLUMNS = "by class"

# The field that names a class rather than measuring one.
CLASS_FIELDS = {"FAR": ("class", "rou_class")}


def by_class(financial_year, scope):
    """Whether this table is presented one column per class."""
    from .bindings import library_table

    version = _version(financial_year)
    table = library_table(version.id if version else None, scope) or {}
    return str(table.get("column_labels") or "").strip().lower().startswith(
        BY_CLASS_COLUMNS)


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
    from .note_library import version_in_force

    return version_in_force(financial_year)


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


def classes(financial_year, scope, field="class"):
    """The asset classes a note is presented in, in the order entered.

    The library's FAR:class - "asset class label, one row per class". They
    are a per-engagement fact, not a library one: one company keeps motor
    vehicles and renovation, the next keeps plant, tooling and moulds, and
    only the register says which.
    """
    rows = DocumentFigure.query.filter_by(
        financial_year_id=financial_year.id, token="FAR", field=field,
        scope=scope or "").order_by(DocumentFigure.id).all()
    return [row.member for row in rows if row.member]


def add_class(financial_year, scope, name, field="class"):
    """Declare a class of asset. Returns the name, or None if it is a repeat."""
    from .audit import record

    name = " ".join(str(name or "").split())
    if not name:
        return None
    # The Total column is always presented and is not a class of asset.
    # Accepting it as one would print it twice and invite somebody to
    # treat it as a fourth kind of equipment.
    if name.lower() == "total":
        return None
    if name in classes(financial_year, scope, field):
        return None

    row = DocumentFigure(financial_year_id=financial_year.id, token="FAR",
                         field=field, scope=scope or "", member=name,
                         text=name)
    db.session.add(row)
    db.session.flush()
    record("document_figure", row.id, "add_class",
           after={"scope": scope, "class": name})
    db.session.commit()
    return name


def remove_class(financial_year, scope, name, field="class"):
    """Drop a class and every figure entered against it.

    Deliberate: a figure belongs to the column it was entered in, and a
    column that no longer exists has nowhere to print. The audit trail
    keeps what went.
    """
    from .audit import record

    rows = DocumentFigure.query.filter_by(
        financial_year_id=financial_year.id, token="FAR",
        scope=scope or "", member=name).all()
    rows += DocumentFigure.query.filter_by(
        financial_year_id=financial_year.id, token="PRIORFS",
        scope=scope or "", member=name).all()
    record("document_figure", 0, "remove_class",
           before={"scope": scope, "class": name, "figures": len(rows)})
    for row in rows:
        db.session.delete(row)
    db.session.commit()


# Where an accumulated depreciation account lands. Its caption names the
# class it belongs to - "Accum Dep - Office Equip" - so reading it as a
# class of its own would offer the preparer the same class twice, once as
# an asset and once as its own contra.
CONTRA_KEYS = {"accumulated_depreciation", "accumulated_amortisation",
               "accumulated_impairment"}

# Bookkeeping words wrapped around a class name in a chart of accounts.
CAPTION_NOISE = ("fixed asset", "fixed assets", "property plant and equipment",
                 "ppe", "intangible asset", "intangible assets",
                 "right of use", "right-of-use")


def suggest_classes(financial_year, note_code):
    """Classes the trial balance already implies, for a preparer to accept.

    A company that keeps separate accounts for computers and motor
    vehicles has already said what its classes are; making somebody retype
    them from the register is work the books have done. Suggestions only -
    the register is what decides, and a class the books never named is
    perfectly normal.

    The accumulated depreciation account is skipped rather than read as a
    class. It maps to the same line as the asset it depreciates and its
    caption names that same class, so taking it at face value would offer
    "Office Equipment" and "Accum Dep - Office Equip" as two classes of
    asset, which is one class and its contra.
    """
    from ..models import TrialBalanceAccount
    from . import bindings, conditions

    figures = bindings.figures_for(financial_year)
    wanted = set(conditions.subject_codes(figures, note_code) or [])
    if not wanted:
        return []

    found = []
    for account in TrialBalanceAccount.query.filter_by(
            financial_year_id=financial_year.id).all():
        if (account.line_code or "") not in wanted:
            continue
        if (account.standard_key or "") in CONTRA_KEYS:
            continue
        name = _class_name(account.account_name)
        if name and name not in found:
            found.append(name)
    return found


def _class_name(caption):
    """A chart of accounts caption with its bookkeeping words removed."""
    name = " ".join(str(caption or "").split())
    lowered = name.lower()
    for noise in CAPTION_NOISE:
        if lowered.startswith(noise + " "):
            name = name[len(noise):].strip(" -,")
            break
    for noise in ("at cost", "- cost", "cost"):
        if name.lower().endswith(noise):
            name = name[:-len(noise)].strip(" -,")
            break
    return name


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


def _settled(text, financial_year):
    """A library instruction with its firm settings filled in.

    "Sum of every band beyond {sicr_days} days past due" is guidance for a
    person, and the number is the whole point of it. Substituted the same
    way the wording in a note is, so the preparer reads "beyond 30 days"
    rather than the name of a setting.
    """
    if not text or "{" not in str(text):
        return text
    from . import note_library, reports

    return reports.render_bindings(note_library._bind_blanks(str(text)),
                                   financial_year.customer, financial_year)


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
        for field in fields:
            field["how"] = _settled(field["how"], financial_year)
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
                group = {
                    "scope": entry["scope"],
                    "heading": entry["note"],
                    "note_code": entry["note_code"],
                    "fields": [dict(field, scope=entry["scope"],
                                    answer=answers.get(field["field"]))
                               for field in wanted],
                    "missing": missing(financial_year, token, entry["scope"]),
                }

                # A movement table presented by class of asset is a grid,
                # not a list: every field is asked once per class and once
                # for the Total column the library always presents. The
                # Total is typed from the register's own total row, not
                # added up here - the engine performs no arithmetic.
                if by_class(financial_year, entry["scope"]):
                    names = classes(financial_year, entry["scope"])
                    group["by_class"] = True
                    group["classes"] = [name for name in names
                                        if name.lower() != "total"] + ["Total"]
                    group["suggested"] = [
                        name for name in suggest_classes(
                            financial_year, entry["note_code"])
                        if name not in names]
                    group["grid"] = [
                        dict(field, cells=[
                            {"member": name,
                             "answer": answers.get((field["field"], name))}
                            for name in group["classes"]])
                        for field in wanted]
                    group["flat"] = []

                document["groups"].append(group)
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

        # A name for each group that a preparer can read. The scope is a
        # table id - N09_PROPERTY_PLANT_EQUIPMENT_T6 - and it was printed
        # beside the heading to tell two tables of the same note apart.
        # It did tell them apart, and it also put template addressing in
        # front of an auditor. Numbered instead, and only where a note
        # really does have more than one table on this page.
        seen = {}
        for group in document["groups"]:
            heading = group.get("heading")
            if heading:
                seen[heading] = seen.get(heading, 0) + 1
        run = {}
        for group in document["groups"]:
            heading = group.get("heading")
            if not heading:
                group["label"] = ""
                continue
            if seen[heading] > 1:
                run[heading] = run.get(heading, 0) + 1
                group["label"] = "%s (table %d of %d)" % (
                    heading, run[heading], seen[heading])
            else:
                group["label"] = heading

        out.append(document)
    return out


def stored(financial_year, token=None, scope=""):
    """{field: DocumentFigure} for one token in one note.

    With no token, everything entered for the engagement keyed by its
    binding and note, so a reviewer's list can show where each came from.
    """
    query = DocumentFigure.query.filter_by(financial_year_id=financial_year.id)
    if token:
        return {(row.field if not row.member else (row.field, row.member)): row
                for row in query.filter_by(
                    token=token, scope=scope_of(token, scope)).all()
                if row.is_answered}
    return {row.where: row for row in query.all() if row.is_answered}


def value(financial_year, token, field, scope="", member=""):
    """The entered figure, or None if nobody has answered."""
    row = DocumentFigure.query.filter_by(
        financial_year_id=financial_year.id, token=token, field=field,
        scope=scope_of(token, scope), member=member or "").first()
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

    # A field presented by class is answered once per column, so it is
    # outstanding until every column has one - and outstanding for certain
    # while nobody has said what the classes are.
    grid = bool(scope) and by_class(financial_year, scope)
    columns = ([name for name in classes(financial_year, scope)
                if name.lower() != "total"] + ["Total"]) if grid else []

    short = []
    for field in wanted:
        if not field["blocking"]:
            continue
        if grid:
            if len(columns) < 2 or any(
                    (field["field"], name) not in answered for name in columns):
                short.append(field)
        elif field["field"] not in answered:
            short.append(field)
    return short


def is_blocking(financial_year, token, field, scope=""):
    for known in catalogue(financial_year, token):
        if known["field"] == field:
            return known["blocking"]
    # A binding the library does not define. Held rather than dropped: an
    # unknown token is a fault in the library or the import, and silently
    # leaving its row out would hide it.
    return True


def save(financial_year, token, field, *, scope="", member="", amount=None,
         text=None, found_at=None, clear=False):
    """Record one figure from a document. Returns the row, or None if cleared.

    Clearing removes the answer, which puts the row back to Incomplete. It
    does not write a zero: a preparer who deletes what they typed has gone
    back to not having answered, and the accounts must say so.
    """
    from flask_login import current_user

    from .audit import record

    scope = scope_of(token, scope)
    member = member or ""
    row = DocumentFigure.query.filter_by(
        financial_year_id=financial_year.id, token=token, field=field,
        scope=scope, member=member).first()
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
                             token=token, field=field, scope=scope,
                             member=member)
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
    """One line per enterable document, for a screen that lists engagements.

    Counted across the groups, not off the document. A scoped document has
    no single list of fields - it has one list per note that uses it, and
    the same twelve field names describe three different asset notes. This
    read a `fields` key that stopped existing when the register grew its
    groups, and took the Documents page down with it for every engagement.
    """
    out = []
    for document in documents(financial_year):
        fields = [field for group in document["groups"]
                  for field in group["fields"]]
        out.append({
            "token": document["token"],
            "name": document["name"],
            "answered": sum(1 for field in fields if field["answer"]),
            "total": len(fields),
            "missing": len(document["missing"]),
        })
    return out
