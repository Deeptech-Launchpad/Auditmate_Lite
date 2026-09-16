"""The library's 29 questions, and the twelve blanks in its wording.

The Preparer inputs sheet is the library's own account of everything a
set of accounts needs that no trial balance holds. Each row names the
note it feeds, when it is worth asking, how it should be handled, and
what becomes of the note if nobody answers.

THREE MODES, and the difference between them is who decides:

  Derive   Something already held settles it, and almost always settles
           it in the negative - no subsidiaries, single currency, nothing
           held at fair value. The preparer sees the conclusion and what
           it was read off, so they can catch it being wrong. There is
           nothing to answer.

  Propose  Something held suggests an answer, and a person confirms or
           corrects it. What is put up is shown as a proposal beside the
           question, never written into the note as though it were
           sourced: accepting it is an act, and the record says whether
           the preparer accepted or typed their own.

  Ask      Only a person knows. A question in plain words, an example,
           and a box.

HOLD AND OMIT come straight off the sheet. Twelve questions hold their
note - unanswered, it prints Incomplete and says which question is
outstanding. Eight omit - the rows drop and the note prints without
them. That is the same distinction the binding engine already draws
between a blocking and a non-blocking field, applied to a question
rather than to a figure.

WHAT THIS DOES NOT DO. The sheet says how many rows each answer fills -
"Rows: 2" - but not which rows, and no note row binds to an item key.
So an answer is recorded, and it holds or omits its note, but nothing
here places a figure into a particular row of a particular table. That
needs the firm to say which binding each item feeds. Until they do,
placing them would mean guessing, and a guessed row is a wrong figure
in a client's accounts.

THE ENGINE STILL DOES NO ARITHMETIC. Five rows of the sheet ask for a
figure the engine has worked out - "the calculated figure, to confirm".
That is the one instruction here not followed as written: the basis is
shown, and the preparer types the figure. See `_basis_only`.
"""
import logging
import re

from ..extensions import db
from ..models import (INPUT_HOLD, INPUT_OMIT, NoteLibraryNote, PreparerInput)
from . import document_fields
from .note_library import normalise_heading

log = logging.getLogger(__name__)

DERIVE, PROPOSE, ASK = "Derive", "Propose", "Ask"

# Rows whose "What the preparer sees" promises a figure the engine has
# calculated. Every printed figure in these accounts is read from a
# source, and a figure the engine worked out is not read from anything -
# so the basis is shown and the preparer supplies the figure. Listed
# rather than detected so that the departure from the library is visible
# and can be undone in one place if the firm rules the other way.
_basis_only = ("DIVIDEND_PER_SHARE", "IR_SENSITIVITY", "CONTRACT_LIAB_REV",
               "KMP_SPLIT", "PPE_UNUSUAL")

# "Asked when", where it does not reduce to whether the note applies.
ALWAYS = "always"
FIRST_TIME_ONLY = "on first-time adoption only"


def _version(financial_year):
    return document_fields._version(financial_year)


def _note_codes(version):
    """Library code for each note, found by its heading.

    The Preparer inputs sheet names its note in words - "Credit risk" -
    while every other sheet uses the code. Folded the same way the
    importer folds a heading, so a reworded heading still matches.
    """
    return {normalise_heading(row.heading): row.library_code
            for row in NoteLibraryNote.query.filter_by(
                library_version_id=version.id).all()}


def _applies(financial_year, note_code, asked_when):
    """Whether this question is worth putting to this preparer.

    Almost every "Asked when" reduces to whether the note applies: a
    company with no borrowings is not asked whether a loan payment was
    missed, because it has no borrowings note. Two do not reduce that
    way and are handled by name.
    """
    when = (asked_when or "").strip().lower()
    if when == ALWAYS:
        return True
    if when == FIRST_TIME_ONLY:
        # Nothing held says whether this is a first FRS set. Asked, with
        # the condition on the question, rather than guessed at either way.
        return True
    return document_fields._note_applies(financial_year, note_code)


def _rows_wanted(raw):
    """How many rows of the note one answer fills. One, unless it says more.

    A question that fills five rows needs five boxes. Giving it one was
    not merely confusing - key management personnel wants short-term
    benefits, employer CPF, other long-term, termination and share-based
    as five separate figures, and there was nowhere to put four of them.
    """
    try:
        return max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        return 1


_CODE = re.compile(r"\b(?:PL|BS|CF|EQ)-[A-Z0-9]+\b")


def _plain(text, labels):
    """The library's own sentence with its line codes said in words.

    "PL-DIRFEE and PL-CPF, plus the directors named on BizFile" is how the
    workbook writes it, and it is correct. It is not how an auditor reads,
    and a page that has to be decoded before it can be answered gets
    answered carelessly. A code with no label is left alone rather than
    mangled into something that looks like a label and is not.
    """
    if not text:
        return text

    def swap(match):
        return labels.get(match.group(0)) or match.group(0)

    return _CODE.sub(swap, str(text))


def catalogue(financial_year):
    """Every question this engagement should see, in the sheet's own words."""
    version = _version(financial_year)
    if version is None:
        return []
    codes = _note_codes(version)
    from . import line_codes

    labels = line_codes.code_labels(financial_year) or {}
    out = []
    for row in version.sheet("Preparer inputs"):
        item = str(row.get("Item") or "").strip()
        if not item:
            continue
        note = str(row.get("Note") or "").strip()
        note_code = codes.get(normalise_heading(note))
        asked_when = str(row.get("Asked when") or "").strip()
        if not _applies(financial_year, note_code, asked_when):
            continue
        mode = str(row.get("Mode") or ASK).strip()
        unanswered = str(row.get("If unanswered") or "").strip()
        out.append({
            "item": item,
            "mode": mode,
            "note": note,
            "note_code": note_code,
            "concludes": _plain(
                str(row.get("What the engine concludes") or "").strip(), labels),
            "from_what": _plain(
                str(row.get("From what") or "").strip(), labels),
            "sees": str(row.get("What the preparer sees") or "").strip(),
            "question": str(row.get("Question, where one is put") or "").strip(),
            "example": str(row.get("Example") or "").strip(),
            "asked_when": asked_when,
            "unanswered": unanswered,
            "holds": unanswered == INPUT_HOLD,
            "omits": unanswered == INPUT_OMIT,
            "rows": str(row.get("Rows") or "").strip(),
            "parts_wanted": _rows_wanted(row.get("Rows")),
            # A proposal is about a figure the books nearly answer, so it
            # gets an amount box. An "Ask" is almost always a sentence -
            # "was any invoice factored" - and showing an empty Amount
            # field beside it invites a number that means nothing.
            "wants_figure": mode == PROPOSE,
            "basis_only": item in _basis_only,
            "first_time_only": asked_when.lower() == FIRST_TIME_ONLY,
        })
    return out


def _headings(version):
    """Plain heading for each note code, so no code reaches the page.

    "Appears in N35_SHAREBASED_PAYMENT_P1" is how a developer reads it.
    An auditor reads "Share-based payment", and the paragraph number is
    not their problem.
    """
    return {row.library_code: row.heading
            for row in NoteLibraryNote.query.filter_by(
                library_version_id=version.id).all() if row.library_code}


def _in_words(used_in, headings):
    """Turn "N35_SHAREBASED_PAYMENT_P1, N44_..." into note names."""
    names = []
    for part in str(used_in or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        # The paragraph suffix - _P1, _P30 - is addressing, not meaning.
        stem = part.rsplit("_P", 1)[0] if "_P" in part else part
        name = headings.get(stem) or headings.get(part)
        if name and name not in names:
            names.append(name)
    return names


def blanks(financial_year):
    """The Fields sheet's blanks that a preparer, and only a preparer, fills.

    Twelve of them, and unlike the 29 they name the exact paragraph they
    sit in. Until now nothing offered them: an unanswered one printed
    "[contingent liability nature not provided]" in the preview with no
    way to provide it.
    """
    version = _version(financial_year)
    if version is None:
        return []
    headings = _headings(version)
    out = []
    for row in version.sheet("Fields"):
        if str(row.get("Supplied by") or "").strip() != "Preparer input":
            continue
        field = str(row.get("Field") or "").strip()
        if not field:
            continue
        blocking = str(row.get("Blocking if unresolved") or "")
        used_in = str(row.get("Used in") or "").strip()
        out.append({
            "item": "field." + field,
            "field": field,
            "mode": ASK,
            "label": field.replace("_", " ").capitalize(),
            "holds": blocking.strip().lower().startswith("hold"),
            "omits": False,
            "holds_text": blocking.strip(),
            "question": str(row.get("What it holds") or "").strip(),
            "format": str(row.get("Format") or "").strip(),
            "used_in": used_in,
            "notes_in_words": _in_words(used_in, headings),
            "rows": "1",
            "parts_wanted": 1,
            "wants_figure": str(row.get("Format") or "").strip() == "Amount",
        })
    return out


def stored(financial_year):
    """{item: PreparerInput} for this engagement."""
    return {row.item: row for row in PreparerInput.query.filter_by(
        financial_year_id=financial_year.id).all()}


def save(financial_year, item, *, mode=ASK, answer=None, amount=None,
         source=None, proposed=None, accepted_proposal=False, parts=None,
         clear=False, user_id=None, commit=True):
    """Record an answer. Returns the row, or None when cleared.

    Clearing is not the same as answering no. It puts the question back to
    unanswered, which puts a holding note back to Incomplete - a company
    that has told us nothing and a company that has told us "none" must
    not read the same way in a set of accounts.
    """
    row = PreparerInput.query.filter_by(
        financial_year_id=financial_year.id, item=item).first()

    if clear:
        if row is not None:
            db.session.delete(row)
            if commit:
                db.session.commit()
        return None

    if row is None:
        row = PreparerInput(financial_year_id=financial_year.id, item=item)
        db.session.add(row)

    row.mode = mode
    row.answer = (answer or "").strip() or None
    row.amount = amount
    row.source = (source or "").strip() or None
    row.proposed = (proposed or "").strip() or None
    row.accepted_proposal = bool(accepted_proposal)
    row.parts = parts or None
    row.decided = True
    row.decided_by = user_id
    if commit:
        db.session.commit()
    return row


def state(financial_year):
    """Every question with its answer, ready for a page or a check."""
    held = stored(financial_year)
    out = []
    for spec in catalogue(financial_year) + blanks(financial_year):
        answer = held.get(spec["item"])
        out.append(dict(spec, answer=answer,
                        answered=bool(answer and answer.is_answered)))
    return out


def outstanding(financial_year, holding_only=False):
    """Questions nobody has answered. Optionally only the ones that hold."""
    return [row for row in state(financial_year)
            if not row["answered"] and (row["holds"] or not holding_only)]


def holds(financial_year):
    """Notes held open by an unanswered question, keyed by note.

    What a checks page and the report engine both want: not "12 things
    are outstanding" but "the borrowings note is waiting on one of them".
    """
    out = {}
    for row in outstanding(financial_year, holding_only=True):
        key = row.get("note") or row.get("used_in") or "Wording"
        out.setdefault(key, []).append(row)
    return out


def _section_codes(section):
    """The library codes a report section covers."""
    binding = section.data_binding or {}
    codes = [str(spec.get("note_code") or "") for spec in
             (binding.get("note_table_specs") or [])]
    codes.append(str(binding.get("library_code") or ""))
    return {code.upper() for code in codes if code}


def holds_for_section(section, financial_year):
    """Reasons this section is held open by an unanswered question.

    The sheet says which note each question feeds and whether leaving it
    unanswered holds that note. A held note prints Incomplete and names
    the question, so the person reading the draft is told what is wanted
    rather than that something is.
    """
    codes = _section_codes(section)
    if not codes:
        return []
    out = []
    for row in outstanding(financial_year, holding_only=True):
        note_code = (row.get("note_code") or "").upper()
        if not note_code or not any(note_code in code or code in note_code
                                    for code in codes):
            continue
        question = row.get("question") or row.get("note") or row["item"]
        out.append("Waiting for the preparer: " + question)
    return out


def values_for_bindings(financial_year):
    """Answers to the Fields sheet's blanks, keyed as the renderer wants.

    render_bindings looks up "field.CONTINGENT_LIABILITY_NATURE". An
    unanswered blank is left out rather than filled with an empty string,
    so the preview keeps saying which one is missing.
    """
    out = {}
    for row in PreparerInput.query.filter_by(
            financial_year_id=financial_year.id).all():
        if not row.is_paragraph_blank or not row.is_answered:
            continue
        text = (row.answer or "").strip()
        if not text and row.amount is not None:
            text = f"{row.amount:,.2f}"
        if text:
            out[row.item] = text
    return out


def summary(financial_year):
    """One line for a page head, and for the engagement list."""
    rows = state(financial_year)
    answered = [r for r in rows if r["answered"]]
    return {
        "total": len(rows),
        "answered": len(answered),
        "outstanding": len(rows) - len(answered),
        "holding": sum(1 for r in rows if not r["answered"] and r["holds"]),
        "by_mode": {mode: sum(1 for r in rows if r["mode"] == mode)
                    for mode in (DERIVE, PROPOSE, ASK)},
    }


def carry_forward(previous, financial_year, user_id=None):
    """Bring last year's answers across, marked as carried.

    A question like "does the company hold anything as security" has the
    same answer most years, and retyping twenty-nine of them is how a
    preparer stops reading them. Carried answers are still answers, and
    the record says which year they came from so a reviewer can see one
    that has not been looked at in three years.
    """
    if previous is None:
        return 0
    have = stored(financial_year)
    moved = 0
    for row in PreparerInput.query.filter_by(
            financial_year_id=previous.id).all():
        if not row.is_answered or row.item in have:
            continue
        db.session.add(PreparerInput(
            financial_year_id=financial_year.id, item=row.item,
            mode=row.mode, decided=True, answer=row.answer,
            amount=row.amount, source=row.source,
            carried_from_id=row.id, decided_by=user_id))
        moved += 1
    db.session.commit()
    return moved
