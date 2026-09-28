"""When a library 2.x note, and each of its paragraphs, applies.

The library says it in two places. The Notes sheet gives each note a tick
state - Always on, TB-driven or Manual. The Paragraphs sheet gives each
paragraph a condition source - what to test to decide whether it prints:

    Unconditional       nothing; prints whenever the note is on
    Table binding       the table decides, row by row (services/bindings.py)
    Line balance        whether a bound line code carries a balance, either year
    Client record       a field on the client record
    Firm setting        a firm-level setting
    Preparer confirms   nothing automatic; offered to the preparer

and what to do when that question goes unanswered: Hold (the note is
incomplete until answered) or Omit (it stays out).

ONE JUDGEMENT OF OUR OWN, and it is a safety one. A paragraph's line codes are
trusted as its test only when every one of them belongs to the paragraph's own
note, by the library's own Statement lines sheet (its "Note code" column), or
to no note at all. Read literally, the file ties "the Company incurred a loss
and its current liabilities exceeded its current assets" to plant and
equipment, "the Company's redeemable preference shares" to share capital, and
foreign currency translation to any other income - each would print a false
statement about a company that merely owns a laptop. A paragraph whose codes
point elsewhere is not tested on them: it is offered to the preparer and, the
library's own rule for unanswered questions, left out until someone says it
applies. Silence is safe; a false assertion is not.

TB-driven notes the same way. A note is on when a line code the Statement
lines sheet assigns to it carries a balance in either year. A note no line
belongs to - offsetting, events after the reporting period, commitments -
falls back to its own trustworthy Line balance paragraphs, and failing those
is the preparer's to switch on.

Nothing here calls the AI.
"""
import re
from decimal import Decimal

PRINT, SKIP, HOLD, OMIT = "print", "skip", "hold", "omit"

_FAMILY = re.compile(r"^N\d+")


def _family(note_code):
    match = _FAMILY.match(note_code or "")
    return match.group(0) if match else None


def is_v2(note):
    """True for a note from a library that states its own conditions."""
    return note.get("tick_state_raw") is not None and any(
        piece.get("condition_source") for piece in _pieces(note))


def _pieces(note):
    return list(note.get("pieces") or [])


def carries_balance(figures, code):
    """A real, non-zero figure this year or last. A held figure is not one."""
    for offset in (0, 1):
        value = figures.resolve(code, offset)
        if isinstance(value, Decimal) and value:
            return True
    return False


def subject_codes(figures, note_code):
    """Line codes the Statement lines sheet assigns to this note."""
    return [code for code, row in figures.lines.items()
            if row.get("Note code") == note_code]


def trusted_codes(piece, note_code, figures):
    """The paragraph's line codes if they can be its test, else None."""
    codes = []
    for code in piece.get("line_codes") or []:
        row = figures.lines.get(code)
        if row is None:
            return None                       # a code the library doesn't define
        if row.get("Statement") in (None, "-"):
            continue                          # CLIENT, FIRM, STATIC: not a line
        owner = row.get("Note code")
        if owner not in (None, "-") and _family(owner) != _family(note_code):
            return None
        codes.append(code)
    return codes or None


def note_applies(note, figures, financial_year):
    """(on, reason) for one note or sub-section."""
    tick = note.get("tick_state_raw")
    code = note.get("library_code")

    if tick == "always":
        return True, "Always on"
    if tick == "manual":
        return False, "The library leaves this note to the preparer"

    trigger = (note.get("trigger_text") or "").strip().lower()
    if trigger == "where a prior period exists":
        if financial_year.is_first_year:
            return False, "First financial period: there is no prior period"
        return True, "A prior period exists"

    subjects = subject_codes(figures, code)
    if subjects:
        live = [c for c in subjects if carries_balance(figures, c)]
        if live:
            return True, "Balance on " + ", ".join(live)
        return False, "No balance on " + ", ".join(subjects)

    tested = False
    for piece in _pieces(note):
        if (piece.get("condition_source") or "").lower() != "line balance":
            continue
        codes = trusted_codes(piece, code, figures)
        if codes is None:
            continue
        tested = True
        live = [c for c in codes if carries_balance(figures, c)]
        if live:
            return True, "Balance on " + ", ".join(live)
    if tested:
        return False, "No balance on the lines this note depends on"
    return False, "No trial balance line decides this note; the preparer does"


def paragraph(piece, note_code, figures, financial_year=None):
    """(action, reason) for one paragraph of a note that is on."""
    source = (piece.get("condition_source") or "").strip().lower()
    # The workbook's own wording for the sources it names. "Firm settings" and
    # "ACRA business profile" were not recognised, so the compilation report's
    # practitioner paragraphs and the directors' names were held as unknown
    # conditions: the firm's details never printed, even when configured.
    if source == "firm settings":
        source = "firm setting"
    elif source == "acra business profile":
        source = "client record"
    elif source.startswith("preparer supplies"):
        source = "preparer confirms"
    tag = (piece.get("tag") or "").upper()
    unanswered = (piece.get("if_unanswered") or "").strip().lower()
    asked = HOLD if unanswered == "hold" else OMIT

    if source in ("unconditional", "table binding", "firm setting"):
        return (HOLD, "Preparer supplies this") if tag == "MANUAL" else (PRINT, "")

    if source == "client record":
        if tag == "TOGGLE":
            # The client record has no field for this yet (a holding
            # company, say), so it cannot be tested and is not assumed.
            return OMIT, "The client record does not hold this yet"
        # The one client-record fact this can actually test today (library
        # feedback A15): GST wording printed for every client regardless of
        # registration, because "client record" otherwise just prints
        # unconditionally below. Matched on the condition's own sentence,
        # not the note, so it only ever touches a paragraph the library
        # itself says is about GST.
        condition_text = (piece.get("condition_text") or "").lower()
        if "gst" in condition_text and financial_year is not None:
            customer = getattr(financial_year, "customer", None)
            registered = bool((getattr(customer, "gst_reg_no", None) or "").strip())
            if registered:
                return PRINT, "The client record shows a GST registration"
            return SKIP, "The client record shows no GST registration"
        return PRINT, ""

    if source == "preparer confirms":
        return asked, "For the preparer to confirm"

    if source == "line balance":
        codes = trusted_codes(piece, note_code, figures)
        if codes is None:
            if tag == "MANUAL":
                return HOLD, "Preparer supplies this"
            return asked, ("Its line codes belong to another note, so they "
                           "cannot decide it; for the preparer to confirm")
        if not any(carries_balance(figures, c) for c in codes):
            return SKIP, "No balance on " + ", ".join(codes)
        if tag == "MANUAL":
            return HOLD, "Preparer supplies this"
        return PRINT, ""

    return HOLD, f"Unknown condition source {piece.get('condition_source')!r}"


# A GST clause riding inside an otherwise-unconditional sentence - "net of
# returns, trade discounts and goods and services tax" - rather than a
# paragraph of its own the library marks as GST-conditional (library
# feedback A15). Checked in the live library version actually loaded for
# an engagement (v3.11): the sentence is one piece of the Revenue note,
# condition_source "Unconditional", with no per-piece hook for a client's
# GST status at all - the note-level "client record" test above never
# gets a chance to run on it. Fixed at the phrase, not the sentence: the
# rest of the sentence (returns, trade discounts) is true regardless of
# GST registration and must stay; only the GST clause is untrue for a
# client with no registration.
#
# Matches only the one confirmed real phrasing - "...and goods and
# services tax" - not a broader pattern for wording nobody has actually
# seen: a guessed pattern that mismatched the grammar around it would
# leave a dangling "of" or "and" behind, which is worse than leaving an
# unrecognised phrasing untouched.
_GST_CLAUSE = re.compile(r"\s*,?\s*and\s+goods and services tax\b", re.I)


def strip_gst_wording(wording, financial_year):
    """`wording`, with an "...and goods and services tax" clause removed
    if the client has no GST registration on file. Unchanged otherwise,
    and unchanged if the phrase is not present at all."""
    if not wording or "goods and services tax" not in wording.lower():
        return wording
    customer = getattr(financial_year, "customer", None) if financial_year else None
    if bool((getattr(customer, "gst_reg_no", None) or "").strip()):
        return wording                                    # registered: true as printed
    return _GST_CLAUSE.sub("", wording)
