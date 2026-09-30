"""Placeholder text a person left in a note, and the draft/final rule.

Covers acceptance criteria G1-G4.

The engine's own markers already had a reason of their own. What it could
not see was a template hole typed or pasted in by a preparer - "{amount}",
"[year]" - which carries no class to find it by. These tests pin both the
detection and, just as importantly, what must NOT be detected: a false
positive here blocks a set of accounts from being issued.
"""
from app.services import completion_needs
from app.services.reports import incomplete_reasons, typed_placeholders

from . import factories as f


# --- what must be found ---------------------------------------------------

def test_finds_lowercase_brace_placeholder():
    assert typed_placeholders("<p>Rent of {amount} was paid.</p>") == ["{amount}"]


def test_finds_uppercase_brace_placeholder():
    """The one brace pattern already in the code was uppercase-only and was
    never used to scan saved wording, so {amount} slipped through. Both
    cases have to be caught."""
    assert typed_placeholders("<p>Rent of {AMOUNT}.</p>") == ["{AMOUNT}"]


def test_finds_bracket_token():
    assert typed_placeholders("<p>For the year ended [year].</p>") == ["[year]"]


def test_finds_tbd():
    assert typed_placeholders("<p>The balance is [TBD].</p>") == ["[TBD]"]


def test_finds_several_and_keeps_order():
    found = typed_placeholders("<p>{amount} paid in [year], balance [TBD].</p>")
    assert found == ["{amount}", "[year]", "[TBD]"]


def test_reports_each_placeholder_once():
    assert typed_placeholders("<p>{amount} then {amount}.</p>") == ["{amount}"]


def test_finds_a_placeholder_split_across_markup():
    """Wording is stored as HTML, so a placeholder can sit inside a tag's
    text while tags surround it."""
    assert typed_placeholders("<p>Rent of <strong>{amount}</strong>.</p>") \
        == ["{amount}"]


# --- what must NOT be found ----------------------------------------------

def test_ignores_the_engines_own_missing_markers():
    """G2. These already produce "Not filled in: ..." through
    _MISSING_BLANK. Reporting them again would show a preparer two
    different reasons for one cell."""
    html = ('<p>namely <span class="missing-binding">'
            '[going concern uncertainty not provided]</span>.</p>')
    assert typed_placeholders(html) == []


def test_ignores_the_engines_markers_with_chip_classes():
    html = ('<p><span class="ph missing-binding" contenteditable="false" '
            'data-ph="field.AMOUNT">[amount not provided]</span></p>')
    assert typed_placeholders(html) == []


def test_ignores_update_marks():
    """[update: ...] is last year's figure carried forward and has its own
    reason."""
    assert typed_placeholders("<p>Rent of [update: 48,000] was paid.</p>") == []


def test_ignores_bracketed_prose():
    """An editorial insertion is not a template hole. Anything reading as a
    sentence is left alone, because a false positive here stops a finished
    set of accounts being issued."""
    assert typed_placeholders(
        "<p>The lease [the Company and its landlord agreed] runs on.</p>") == []


def test_ignores_ordinary_accounting_wording():
    """Real wording off a live report - parentheses, percentages, currency,
    comparatives - must not trip it."""
    html = ("<p>Tax calculated at a tax rate of 17% (2024: 17%). Revenue of "
            "S$1,859,158 (2024: S$738,845) less cost of sales of S$942,224. "
            "Interest is charged at 5.5% per annum.</p>")
    assert typed_placeholders(html) == []


def test_scans_rendered_output_not_stored_binding_templates():
    """The important false-positive vector, found while testing this on real
    data. A note's STORED wording is full of "{ customer.legal_name }" and
    "{ firm.default_days }" - those are the engine's own binding templates,
    substituted by render_bindings() before anything prints. This check runs
    on rendered output, where they are already values, so a clean report
    flags nothing. Scanning stored wording instead would flag every note on
    every engagement and block approval across the board.
    """
    rendered = "<p>Marina Bay Trading Pte Ltd, incorporated in Singapore.</p>"
    assert typed_placeholders(rendered) == []


def test_a_binding_that_leaks_unresolved_is_still_caught():
    """The other side of it: if a binding ever reaches the page unresolved
    and without the engine's own class on it, that IS a template hole in
    client-facing output and should be reported."""
    leaked = "<p>{ customer.legal_name }, incorporated in Singapore.</p>"
    assert typed_placeholders(leaked) == ["{ customer.legal_name }"]


def test_ignores_empty_brackets():
    assert typed_placeholders("<p>Nothing here [].</p>") == []


def test_ignores_html_attributes():
    """Tags are stripped before scanning, so an attribute value that happens
    to contain braces is not wording."""
    assert typed_placeholders('<p data-x="{not_wording}">Clean text.</p>') == []


# --- how it reads to the preparer ----------------------------------------

def test_classified_as_its_own_reason_not_the_generic_fallback():
    """The mistake already made once with the A2 safety net: an unmatched
    reason collapses into "the items shown in the note", which tells the
    preparer nothing."""
    reason = "Placeholder left in the wording: {amount}"
    assert completion_needs.classify(reason) == "placeholder"


def test_summary_names_the_placeholder_problem():
    lines = completion_needs.summarise(
        ["Placeholder left in the wording: {amount}"])
    assert lines
    joined = " ".join(lines)
    assert "placeholder" in joined.lower()
    assert "the items shown in the note" not in joined


def test_reaches_the_sections_incomplete_reasons(db):
    """The integration point that gates approval: a section whose rendered
    wording carries a typed placeholder must come back incomplete."""
    rep = f.report(note_keys=("provisions",))
    section = rep.sections[0]
    payload = {"html": "<p>A provision of {amount} was recognised.</p>"}

    reasons = incomplete_reasons(section, payload)
    assert any(r.startswith("Placeholder left in the wording:") for r in reasons)
    assert "Placeholder left in the wording: {amount}" in reasons


def test_clean_wording_leaves_the_section_complete(db):
    rep = f.report(note_keys=("provisions",))
    section = rep.sections[0]
    payload = {"html": "<p>A provision of S$12,600 was recognised.</p>"}
    assert incomplete_reasons(section, payload) == []


def test_engine_marker_gives_one_reason_not_two(db):
    """G2 at the integration level. The engine's own marker must produce
    "Not filled in" and nothing else."""
    rep = f.report(note_keys=("going_concern",))
    section = rep.sections[0]
    payload = {"html": ('<p>namely <span class="missing-binding">'
                        '[going concern uncertainty not provided]</span>.</p>')}

    reasons = incomplete_reasons(section, payload)
    assert len(reasons) == 1
    assert reasons[0].startswith("Not filled in:")


def test_a_placeholder_makes_the_report_incomplete(db):
    """G4. `finalise` refuses to close the engagement when
    record_completeness returns anything, so this list being non-empty IS
    the block."""
    from app.services.reports import record_completeness

    rep = f.report(note_keys=("provisions",))
    section = rep.sections[0]
    payload = {"section": section, "html": "<p>A provision of {amount}.</p>"}
    payload["incomplete"] = incomplete_reasons(section, payload)

    incomplete = record_completeness(rep, [payload])
    assert incomplete, "a typed placeholder must hold the report incomplete"
    titles = [title for title, _reasons in incomplete]
    assert section.title in titles


def test_clean_report_is_not_held_incomplete_by_this_check(db):
    from app.services.reports import record_completeness

    rep = f.report(note_keys=("provisions",))
    section = rep.sections[0]
    payload = {"section": section, "html": "<p>A provision of S$12,600.</p>"}
    payload["incomplete"] = incomplete_reasons(section, payload)

    assert record_completeness(rep, [payload]) == []


def test_appears_in_the_grouped_needs_list():
    needs = completion_needs.needs(
        [("Provisions", ["Placeholder left in the wording: [year]"])])
    keys = [group["key"] for group in needs]
    assert "placeholder" in keys
    group = next(g for g in needs if g["key"] == "placeholder")
    assert group["owner"] == "The preparer"
    assert "Provisions" in group["notes"]
