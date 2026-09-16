"""The checks a person runs on a draft before it is signed.

Library 3.5 took the arithmetic out of the engine. Every printed figure is
now whatever its source states, totals included: AuditMate does not add
rows to produce a total and does not add them to check one. That decision
leaves a gap, and the library fills it deliberately rather than by
accident - the Preparer checks sheet, whose own words are:

    The engine performs no arithmetic, so these are not gates. They are
    the checks a person runs on the draft before it is signed, printed as
    a list alongside it. Each one names the figures to compare and what a
    difference means.

So this page is not a validator. It does not compute a difference, flag a
failure, or hold anything back - there is no pass and no fail on it. What
it does is save the preparer the hunt: for each check it gathers, from
this draft, the figures the check names and prints them beside each other,
each one quoted from where it already prints. Comparing them is the
person's job, and doing it on a printed page beside the accounts is how an
audit file is meant to work.

Where the source a check names is not loaded yet - the signed prior year
accounts, the fixed asset register, the aged listing - the check still
prints, and says plainly that the document is not here. A check silently
dropped because its input is missing is worse than no check at all: it
reads as if it passed.

The page belongs to the working copy. It is the last page of a draft, and
it is absent from the clean copy the client receives, which is the
accounts and not the working papers.

Nothing here calls the AI.
"""
import logging

from .bindings import COMPOSITE_LINES, figures_for
from ..models import FinancialStatement, NoteLibraryVersion

log = logging.getLogger(__name__)

# The row on the Preparer checks sheet that separates the ten checks from
# the row-level arithmetic list beneath them.
ROW_ARITHMETIC_MARKER = "row-level arithmetic"

# Which line codes each check is about. The library names the check in
# words ("the charge in the fixed asset note, the items charged note and
# the cash flow statement"); this says which codes those are, so the page
# can show every place in this draft that a code prints and let the
# preparer see three presentations of one figure at once.
CHECK_CODES = {
    "PC-04": ["PL-DEP", "PL-DEPROU", "CF-DEP"],
    "PC-05": ["PL-DIRFEE", "PL-STAFF"],
    "PC-06": ["PL-TAX", "BS-TAXPAY", "CF-TAXPAID"],
    "PC-09": ["BS-TA", "BS-TEL"],
}

# Checks whose second figure comes from a document the engine does not
# read yet. Named here so the page says which document, rather than
# printing one figure and leaving the reader to wonder what happened to
# the other.
AWAITING = {
    "PC-03": "last year's signed accounts",
    "PC-07": "the fixed asset register",
    "PC-08": "the aged receivables listing",
}


def _sheet(financial_year, name):
    version = None
    if getattr(financial_year, "library_version_id", None):
        version = NoteLibraryVersion.query.get(financial_year.library_version_id)
    if version is None:
        version = NoteLibraryVersion.query.filter_by(status="active").first()
    if version is None:
        return None, []
    return version, (version.sheet(name) or [])


def _split(rows):
    """(the ten checks, the row-arithmetic entries).

    Both halves of the sheet share four column headings, so they are told
    apart by the marker row between them rather than by their shape.
    """
    checks, sums, past_marker = [], [], False
    for row in rows:
        ref = str(row.get("Ref") or "").strip()
        if not ref:
            continue
        if ROW_ARITHMETIC_MARKER in ref.lower():
            past_marker = True
            continue
        if ref.lower() == "table":                    # the sub-heading row
            continue
        (sums if past_marker else checks).append(row)
    return checks, sums


# --------------------------------------------------------------------------
# What this draft actually prints
# --------------------------------------------------------------------------

def _places(payloads, figures):
    """Every figure in the draft, with the line codes behind it.

    Built from the rendered payloads rather than from the library, so what
    the page quotes is what the reader is holding - an overridden figure
    appears here as the figure that prints, which is the whole point of
    PC-10.
    """
    places = []
    for payload in payloads or []:
        section = payload.get("section")
        title = getattr(section, "title", "") or ""
        for table in payload.get("tables") or []:
            heading = table.get("heading") or title
            for row in table.get("rows") or []:
                places.append({
                    "where": title,
                    "heading": heading,
                    "what": row.get("label"),
                    "amount": row.get("current"),
                    "held": row.get("held_current"),
                    "codes": figures.codes_in(row.get("from_binding") or ""),
                    "typed": bool(row.get("override_record")),
                })
    return places


def _statement_lines(financial_year, figures):
    """The face of the statements, keyed by the line codes that roll into it."""
    lines = []
    for statement in FinancialStatement.query.filter_by(
            financial_year_id=financial_year.id).all():
        if statement.statement_type == "trial_balance":
            continue                                  # not part of the draft
        for line in statement.lines:
            keys = {line.line_key}
            codes = [code for code in figures.lines
                     if _keys_of(figures, code) & keys]
            lines.append({
                "where": statement.type_label,
                "heading": statement.type_label,
                "what": line.effective_label,
                "amount": line.effective_amount,
                "held": None,
                "codes": codes,
                "typed": bool(line.override_record),
            })
    return lines


def _keys_of(figures, code):
    """The statement line keys one line code prints on.

    Totals and results - total assets, profit before tax - have no
    accounts of their own, so the categories know nothing about them. The
    statements compute them, and bindings.py already records which line
    each one is; PC-09 compares two of them, so it is read from there.
    """
    return figures.keys_for_code(code) or set(COMPOSITE_LINES.get(code) or [])


def _appearances(places, codes):
    """Every place in the draft one of these line codes prints."""
    wanted, found = set(codes), []
    for place in places:
        if wanted & set(place["codes"] or []):
            found.append(place)
    return found


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

def build(report, financial_year, payloads):
    """Everything the Preparer checks page prints. No figure is computed."""
    from . import overrides as overrides_service

    version, rows = _sheet(financial_year, "Preparer checks")
    if not rows:
        return None
    checks_sheet, sums_sheet = _split(rows)

    figures = figures_for(financial_year)
    places = _places(payloads, figures) + _statement_lines(financial_year, figures)

    checks = []
    for row in checks_sheet:
        ref = str(row.get("Ref") or "").strip()
        check = {
            "ref": ref,
            "name": row.get("Check") or "",
            "compare": row.get("Compare") or "",
            "means": row.get("A difference means") or "",
            "figures": [],
            "waiting_on": AWAITING.get(ref),
            "note": None,
        }
        if ref in CHECK_CODES:
            check["figures"] = _appearances(places, CHECK_CODES[ref])
            if not check["figures"]:
                check["note"] = ("Nothing in this draft prints these figures, "
                                 "so there is nothing to compare.")
        elif ref == "PC-01":
            check["pairs"] = _note_totals(payloads, financial_year, figures)
        elif ref == "PC-02":
            check["tables"] = _row_sums(sums_sheet, payloads)
        elif ref == "PC-10":
            check["overrides"] = overrides_service.for_report(
                report, include_cleared=False)
            if not check["overrides"]:
                check["note"] = "No figure or sentence in this draft was typed over."
        checks.append(check)

    return {
        "version": version.version_label if version else None,
        "checks": checks,
        "count": len(checks),
    }


def _note_totals(payloads, financial_year, figures):
    """PC-01: each note total beside the line the library says it must equal.

    The pair is two quotations - the total as the note prints it, and the
    line as the statements print it. They are not subtracted here. Where
    the engine filled the total by reading that same line they will agree
    by construction; where a person typed the total, or the engine held
    it, they may not, and that is the pair worth reading.

    Compared at the level of the printed line, not the line code. Seven
    codes make up trade and other receivables and the balance sheet prints
    one line for them, so listing the face figure once per code would
    repeat 435,463 five times over and read as a sum five times too big.
    The codes are resolved to the statement lines they roll into, and each
    line is named once.
    """
    from .bindings import library_table

    face = _face_lines(financial_year)
    pairs = []
    for payload in payloads or []:
        section = payload.get("section")
        specs = (getattr(section, "data_binding", None) or {}).get(
            "note_table_specs") or []
        by_id = {spec.get("table_id"): spec for spec in specs}
        for table in payload.get("tables") or []:
            spec = by_id.get(table.get("table_id"))
            if not spec:
                continue
            library = library_table(spec.get("version_id"),
                                    spec.get("table_id")) or {}
            named = str(library.get("totals_agree_with") or "").strip()
            if not named or named.lower() in ("no total", "-"):
                continue

            keys, unknown = [], []
            for code in named.replace("+", " ").split():
                if code not in figures.lines:
                    continue
                found = _keys_of(figures, code)
                if not found:
                    unknown.append(figures.label(code))
                for key in sorted(found):
                    if key not in keys:
                        keys.append(key)

            # A general code such as PL-ADM covers every administrative
            # category, so the face prints a dozen lines for it and most of
            # them are nil. A nil line cannot change the comparison, so it
            # is left out and counted - the reader can still tie the list
            # back to the statement, and the row stays readable.
            shown = [face[key] for key in keys if key in face]
            lines = [line for line in shown if line["amount"]]
            nil = len(shown) - len(lines)

            total_row = next((r for r in reversed(table.get("rows") or [])
                              if r.get("bold") or r.get("rule")), None)

            # Nothing to compare on either side: the note prints no total
            # and the statements print no line for it. Saying so would be
            # fine; printing two dashes beside each other is not.
            if total_row is None and not lines:
                continue

            pairs.append({
                "note": getattr(section, "title", ""),
                "table": table.get("heading"),
                "has_total": total_row is not None,
                "note_total": (total_row or {}).get("current"),
                "note_total_held": (total_row or {}).get("held_current"),
                "agrees_with": named,
                "lines": lines,
                "nil_lines": nil,
                "unknown": unknown,
            })
    pairs.sort(key=lambda pair: not pair["has_total"])
    return pairs


def _face_lines(financial_year):
    """{statement line key: what the statements print for it}.

    Read back off the rendered line rather than re-totalled from the trial
    balance, so a figure overridden on the face shows here as the figure
    the reader is holding.
    """
    face = {}
    for statement in FinancialStatement.query.filter_by(
            financial_year_id=financial_year.id).all():
        if statement.statement_type == "trial_balance":
            continue
        for line in statement.lines:
            face.setdefault(line.line_key, {
                "label": line.effective_label,
                "amount": line.effective_amount,
                "where": statement.type_label,
            })
    return face


def _row_sums(sums_sheet, payloads):
    """PC-02: the tables whose total used to be computed, and their rows.

    Nineteen rows on the library's own list were formulas until version
    3.0 - a total of the rows above, a difference between two of them.
    They are now read from their source like every other figure, which
    means nothing in the engine would notice if a source stated a total
    its own components do not support. The test client's unaudited set
    printed 5,100 against components of 6,600, which is exactly the fault
    this catches.

    So the rows and the total are printed together, in the order they
    appear in the accounts, and a person adds them up.
    """
    wanted = {}
    for row in sums_sheet:
        table_id = str(row.get("Ref") or "").strip()
        wanted.setdefault(table_id, []).append({
            "row": row.get("Check"),
            "label": row.get("Compare"),
            "was": row.get("A difference means"),
        })

    listed = []
    for payload in payloads or []:
        section = payload.get("section")
        for table in payload.get("tables") or []:
            table_id = table.get("table_id")
            if table_id not in wanted or not table.get("rows"):
                continue                # held or not shown: nothing to add up
            note = getattr(section, "title", "")
            heading = table.get("heading")
            listed.append({
                "note": note,
                "table_id": table_id,
                "heading": heading if heading and heading != note else None,
                "rows": [{"label": r.get("label"),
                          "amount": r.get("current"),
                          "held": r.get("held_current"),
                          "is_total": bool(r.get("bold") or r.get("rule")),
                          "typed": bool(r.get("override_record"))}
                         for r in table.get("rows") or []],
                "formerly": wanted[table_id],
            })
    return listed
