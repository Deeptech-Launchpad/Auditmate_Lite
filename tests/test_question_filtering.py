"""Per-note questions and figures: the code-resolution layer.

Covers acceptance criteria B1-B4.

`auditmate_test` starts empty except for schema - never seeded with a real
library workbook import. An earlier version of this file queried
`NoteLibraryVersion.query.get(6)` assuming whatever happened to be imported
in a developer's own `auditmate_dev` would also exist here; it does not,
and depending on it made these tests fail (or hang, behind a stale
connection left idle-in-transaction from an earlier run) for a reason with
nothing to do with the code under test. `factories.library_version()``
builds the minimum a test actually needs instead: a handful of notes and a
"Preparer inputs" sheet, in the same shape a real import produces.

The three real catalogue rows this reproduces (JUDGEMENT_SUBJECT naming a
short form of a note's real heading, SUPPORT_PARTY naming two notes at
once, CASH_FLOW_FIGURES naming a STATEMENT rather than a note) are taken
from the actual v3.11 library workbook, read once by hand - not invented.
"""
from app.services import preparer_inputs as prep

from . import factories as f


def _v311_preparer_inputs():
    """The three rows from the real library that exercise every case:
    a plain resolve, a short-form alias, a compound value, and a row that
    genuinely names no note at all."""
    common = {"Mode": "Ask", "Asked when": "Every engagement",
              "If unanswered": "Ask again", "Rows": "1",
              "What the engine concludes": "", "From what": "",
              "What the preparer sees": "", "Question, where one is put": "",
              "Example": ""}
    return [
        dict(common, Item="GOING_CONCERN_Q", Note="Going concern"),
        dict(common, Item="JUDGEMENT_SUBJECT", Note="Critical judgements"),
        dict(common, Item="SUPPORT_PARTY",
             Note="Directors' statement; Going concern"),
        dict(common, Item="CASH_FLOW_FIGURES", Note="Statement of cash flows"),
    ]


def _v311_notes():
    return [
        ("going_concern", "N03_GOING_CONCERN", "Going concern"),
        ("critical_judgements", "N39_CRITICAL_JUDGEMENTS_APPLYI",
         "Critical judgements in applying accounting policies"),
        ("directors_statement", "S01_DIRECTORS_STATEMENT",
         "Directors' statement"),
        ("provisions", "N20_PROVISIONS", "Provisions"),
        ("provisions_longterm", "N20_PROVISIONS_LONGTERM",
         "Long-term provisions"),
    ]


def _fy_with_library(db_):
    fy = f.financial_year()
    version = f.library_version(notes=_v311_notes(),
                                preparer_inputs=_v311_preparer_inputs())
    fy.library_version_id = version.id
    db_.session.flush()
    return fy


# --- the stem bug -----------------------------------------------------------

def test_paragraph_suffix_is_only_digits():
    """The bug found while generalising: the old rule stripped "_P" as a
    bare substring, so "N11_INTANGIBLE_ASSETS_POL" - a real code ending in
    _POL, not a paragraph reference - was truncated to
    "N11_INTANGIBLE_ASSETS", a different, real code. Silently pointing a
    field's applicability at the wrong note is worse than not stemming it
    at all."""
    assert prep._stems("N11_INTANGIBLE_ASSETS_POL") == \
        ["N11_INTANGIBLE_ASSETS_POL"]
    assert prep._stems("N19_INVENTORIES_POL") == ["N19_INVENTORIES_POL"]


def test_paragraph_suffix_is_still_stripped():
    assert prep._stems("N49_CONTINGENT_LIABILITIES_P2") == \
        ["N49_CONTINGENT_LIABILITIES"]


def test_stems_are_deduplicated_in_order():
    assert prep._stems("N20_PROVISIONS_P1, N20_PROVISIONS_P4, N19_INVENTORIES_P1") \
        == ["N20_PROVISIONS", "N19_INVENTORIES"]


def test_stems_split_on_comma_or_semicolon():
    assert prep._stems("N49_CONTINGENT_LIABILITIES_P2; N50_CONTINGENT_ASSETS_P2") \
        == ["N49_CONTINGENT_LIABILITIES", "N50_CONTINGENT_ASSETS"]


# --- the three real cases from v3.11 ----------------------------------------

def test_plain_heading_resolves(db):
    codes = {"goingconcern": "N03_GOING_CONCERN"}
    assert prep._resolve_note_codes("Going concern", codes) == \
        ["N03_GOING_CONCERN"]


def test_critical_judgements_alias_resolves(db):
    """JUDGEMENT_SUBJECT's sheet value is short for the note's real
    heading - "Critical judgements" vs "Critical judgements in applying
    accounting policies"."""
    codes = {"criticaljudgementsinapplyingaccountingpolicies":
             "N39_CRITICAL_JUDGEMENTS_APPLYI"}
    assert prep._resolve_note_codes("Critical judgements", codes) == \
        ["N39_CRITICAL_JUDGEMENTS_APPLYI"]


def test_compound_note_value_resolves_to_both(db):
    """SUPPORT_PARTY's sheet value names two notes at once."""
    codes = {"directorsstatement": "S01_DIRECTORS_STATEMENT",
             "goingconcern": "N03_GOING_CONCERN"}
    resolved = prep._resolve_note_codes(
        "Directors' statement; Going concern", codes)
    assert resolved == ["S01_DIRECTORS_STATEMENT", "N03_GOING_CONCERN"]


def test_a_statement_not_a_note_resolves_to_nothing(db):
    """CASH_FLOW_FIGURES names "Statement of cash flows" - a statement, not
    a disclosure note. Genuinely nothing to resolve to; it is not a bug to
    leave this one without a note_code, and it must not raise."""
    codes = {"goingconcern": "N03_GOING_CONCERN"}
    assert prep._resolve_note_codes("Statement of cash flows", codes) == []


def test_all_but_one_row_resolves_a_note(db):
    fy = _fy_with_library(db)
    rows = prep.catalogue(fy)
    unresolved = [r["item"] for r in rows if not r["note_codes"]]
    assert unresolved == ["CASH_FLOW_FIGURES"]


def test_unresolved_item_still_appears_in_the_full_catalogue(db):
    """It has no note to filter by, but it must not vanish from the
    unfiltered list - that would be a silent loss of a real question."""
    fy = _fy_with_library(db)
    rows = prep.catalogue(fy)
    item = next((r for r in rows if r["item"] == "CASH_FLOW_FIGURES"), None)
    assert item is not None
    assert item["note_codes"] == []


def test_support_party_carries_both_codes(db):
    fy = _fy_with_library(db)
    rows = prep.catalogue(fy)
    item = next(r for r in rows if r["item"] == "SUPPORT_PARTY")
    assert set(item["note_codes"]) == {"S01_DIRECTORS_STATEMENT",
                                       "N03_GOING_CONCERN"}


# --- codes_for() and filter_by_note(): the unified shape --------------------

def test_codes_for_reads_catalogue_rows(db):
    fy = _fy_with_library(db)
    rows = prep.catalogue(fy)
    row = next(r for r in rows if r["item"] == "JUDGEMENT_SUBJECT")
    assert prep.codes_for(row) == {"N39_CRITICAL_JUDGEMENTS_APPLYI"}


def test_codes_for_reads_blank_rows():
    row = {"note_codes": ["N49_CONTINGENT_LIABILITIES"]}
    assert prep.codes_for(row) == {"N49_CONTINGENT_LIABILITIES"}


def test_filter_by_note_keeps_only_matching_rows(db):
    fy = _fy_with_library(db)
    rows = prep.state(fy)
    filtered = prep.filter_by_note(rows, "N03_GOING_CONCERN")
    assert filtered
    assert all("N03_GOING_CONCERN" in prep.codes_for(r) for r in filtered)
    assert len(filtered) < len(rows)


def test_filter_by_note_with_no_code_returns_everything(db):
    fy = _fy_with_library(db)
    rows = prep.state(fy)
    assert prep.filter_by_note(rows, "") == rows
    assert prep.filter_by_note(rows, None) == rows


def test_filter_by_note_with_an_unused_code_returns_nothing(db):
    fy = _fy_with_library(db)
    rows = prep.state(fy)
    assert prep.filter_by_note(rows, "N00_NOT_A_REAL_NOTE") == []


# --- section_note_codes() and the exact-match fix on holds_for_section -----

def test_a_table_less_section_resolves_by_heading(db):
    """"Corporate information" has no note_table_specs at all - it is pure
    wording - so it must fall back to resolving by its own title."""
    fy = _fy_with_library(db)
    section = f.report(fy=fy, note_keys=("corporate_information",)).sections[0]
    section.title = "Going concern"
    codes = prep.section_note_codes(section, fy)
    assert "N03_GOING_CONCERN" in codes


def test_provisions_no_longer_cross_matches_longterm(db):
    """B4, the exact bug. A substring test made "N20_PROVISIONS" match
    "N20_PROVISIONS_LONGTERM" as well, holding the wrong note open."""
    fy = _fy_with_library(db)
    rep = f.report(fy=fy, note_keys=("provisions",))
    section = rep.sections[0]
    section.data_binding = {"note_table_specs": [
        {"note_code": "N20_PROVISIONS_LONGTERM"}]}

    row = {"note_codes": ["N20_PROVISIONS"]}
    codes = prep.section_note_codes(section, fy)
    assert not (prep.codes_for(row) & codes), \
        "N20_PROVISIONS must not match N20_PROVISIONS_LONGTERM"


def test_holds_for_section_only_reports_its_own_questions(db, monkeypatch):
    fy = _fy_with_library(db)
    rep = f.report(fy=fy, note_keys=("going_concern",))
    section = rep.sections[0]
    section.data_binding = {"note_table_specs": [
        {"note_code": "N03_GOING_CONCERN"}]}

    def fake_outstanding(financial_year, holding_only=False):
        return [
            {"note_codes": ["N03_GOING_CONCERN"], "holds": True,
             "question": "Is there a material uncertainty?",
             "note": "Going concern", "item": "GC_Q", "answered": False},
            {"note_codes": ["N20_PROVISIONS"], "holds": True,
             "question": "Provision basis?", "note": "Provisions",
             "item": "PROV_Q", "answered": False},
        ]
    monkeypatch.setattr(prep, "outstanding", fake_outstanding)

    reasons = prep.holds_for_section(section, fy)
    assert len(reasons) == 1
    assert "material uncertainty" in reasons[0]
