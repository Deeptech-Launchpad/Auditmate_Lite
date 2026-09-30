"""Per-note wording source: last year's FS or the notes library.

Covers acceptance criteria E1-E3, plus the roll_forward_references fix
that came out of removing the dead carry_forward_prior_wording() path.
"""
import pytest

from app.extensions import db
from app.models import AuditReportSection, PriorYearNote
from app.services import reports as report_service

from . import factories as f


def _section(library_html="<p>Library wording.</p>",
             prior_html="<p>Last year's wording.</p>",
             wording_source="library", content_html=None):
    rep = f.report(note_keys=("revenue",))
    section = rep.sections[0]
    section.library_html = library_html
    section.prior_html = prior_html
    section.wording_source = wording_source
    section.content_html = content_html if content_html is not None else (
        library_html if wording_source == "library" else prior_html)
    db.session.flush()
    return section


# --- wording_sources(): what there is to choose from -----------------------

def test_both_sources_offered_when_both_exist(db):
    section = _section()
    assert report_service.wording_sources(section) == ["library", "prior"]


def test_only_library_when_note_absent_from_last_years_fs(db):
    """E3. A note that was not in last year's FS has prior_html = None and
    must offer no toggle to switch to it."""
    section = _section(prior_html=None)
    assert report_service.wording_sources(section) == ["library"]


def test_only_prior_when_the_library_has_dropped_the_note(db):
    section = _section(library_html=None, wording_source="prior")
    assert report_service.wording_sources(section) == ["prior"]


def test_neither_for_a_non_note_section(db):
    """A statement or the cover page has never had either column set."""
    section = _section(library_html=None, prior_html=None,
                       wording_source=None, content_html="<p>x</p>")
    assert report_service.wording_sources(section) == []


# --- wording_is_edited(): has content_html drifted? ------------------------

def test_unedited_library_wording_reads_as_not_edited(db):
    section = _section(wording_source="library")
    assert report_service.wording_is_edited(section) is False


def test_editing_the_current_sources_text_is_detected(db):
    section = _section(wording_source="library")
    section.content_html = "<p>Something the preparer typed instead.</p>"
    assert report_service.wording_is_edited(section) is True


def test_no_wording_source_reads_as_not_edited(db):
    """Nothing to have drifted FROM - a statement, or a note built before
    this feature existed and never yet switched."""
    section = _section(wording_source=None,
                       content_html="<p>Whatever this holds.</p>")
    assert report_service.wording_is_edited(section) is False


# --- switch_wording_source(): E1 and E2 -------------------------------------

def test_switching_to_prior_replaces_content_and_records_the_source(db):
    section = _section(wording_source="library")
    result = report_service.switch_wording_source(section, "prior")
    assert result["ok"] is True
    assert section.wording_source == "prior"
    assert section.content_html == "<p>Last year's wording.</p>"


def test_switching_back_to_library_works_the_other_way(db):
    section = _section(wording_source="prior",
                       content_html="<p>Last year's wording.</p>")
    result = report_service.switch_wording_source(section, "library")
    assert result["ok"] is True
    assert section.wording_source == "library"
    assert section.content_html == "<p>Library wording.</p>"


def test_switching_with_no_edit_needs_no_confirmation(db):
    section = _section(wording_source="library")
    result = report_service.switch_wording_source(section, "prior")
    assert result["ok"] is True
    assert "needs_confirm" not in result


def test_an_edit_is_never_silently_discarded_on_switch(db):
    """E2, the core guarantee. An edit away from the current source blocks
    the switch until force=True is passed back."""
    section = _section(wording_source="library")
    section.content_html = "<p>The preparer's own sentence.</p>"

    blocked = report_service.switch_wording_source(section, "prior")
    assert blocked["ok"] is False
    assert blocked["needs_confirm"] is True
    # nothing changed
    assert section.wording_source == "library"
    assert section.content_html == "<p>The preparer's own sentence.</p>"

    forced = report_service.switch_wording_source(section, "prior", force=True)
    assert forced["ok"] is True
    assert section.wording_source == "prior"
    assert section.content_html == "<p>Last year's wording.</p>"


def test_switching_to_a_source_with_nothing_to_offer_is_refused(db):
    """E3 at the service layer: switching to "prior" when the note was
    never in last year's FS must fail cleanly, not write None over the
    printed wording."""
    section = _section(prior_html=None, wording_source="library")
    result = report_service.switch_wording_source(section, "prior")
    assert result["ok"] is False
    assert section.content_html == "<p>Library wording.</p>"  # untouched


def test_unknown_source_is_refused(db):
    section = _section()
    result = report_service.switch_wording_source(section, "not-a-real-source")
    assert result["ok"] is False


# --- the endpoint ------------------------------------------------------------

def test_switch_endpoint_succeeds(auth_client, db):
    section = _section(wording_source="library")
    response = auth_client.patch(
        f"/reports/api/section/{section.id}/wording-source",
        json={"source": "prior"})
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["content_html"] == "<p>Last year's wording.</p>"


def test_switch_endpoint_asks_before_discarding_an_edit(auth_client, db):
    section = _section(wording_source="library")
    section.content_html = "<p>Edited by the preparer.</p>"
    db.session.commit()

    response = auth_client.patch(
        f"/reports/api/section/{section.id}/wording-source",
        json={"source": "prior"})
    assert response.status_code == 400
    assert response.get_json()["needs_confirm"] is True

    response = auth_client.patch(
        f"/reports/api/section/{section.id}/wording-source",
        json={"source": "prior", "force": True})
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


# --- roll_forward_references(), now gated on wording_source -----------------

@pytest.fixture()
def fy_with_prior_note_8(db):
    """Last year's accounts numbered the revenue note "8"; this year it is
    note 1 (the only note in the report)."""
    fy = f.financial_year()
    rep = f.report(fy=fy, note_keys=("revenue",))
    db.session.add(PriorYearNote(
        financial_year_id=fy.id, note_number="8", title="Revenue",
        matched_key="revenue", body_text="Revenue is recognised..."))
    db.session.flush()
    return fy, rep


def test_reference_is_rewritten_when_source_is_prior(fy_with_prior_note_8):
    fy, rep = fy_with_prior_note_8
    section = rep.sections[0]
    section.wording_source = "prior"
    section.content_html = "<p>As explained in Note 8, revenue is recognised.</p>"
    db.session.flush()

    html = report_service.roll_forward_references(
        section.content_html, fy, section)
    assert "Note 1" in html
    assert "Note 8" not in html


def test_reference_is_not_touched_when_source_is_library(fy_with_prior_note_8):
    """The bug the old prior_note_id gate had exactly backwards is avoided
    here in the other direction: library wording was never carried from
    last year and must not have its own numbers hunted for stale
    references it never had."""
    fy, rep = fy_with_prior_note_8
    section = rep.sections[0]
    section.wording_source = "library"
    section.content_html = "<p>See Note 8 for something unrelated.</p>"
    db.session.flush()

    # section_payload() is what actually gates this in the render path.
    payload = report_service.section_payload(section, rep.financial_year.customer,
                                             fy, chips=False)
    assert "Note 8" in payload["html"]   # untouched, because gated on library
