"""ACRA / standards-change alert (F): a note's library wording has moved on
since the engagement was pinned.

The library's own catalogue-building (note_library.build_catalogue) is
exercised elsewhere; these tests stub it so what's actually new here -
comparing two versions, the honesty-rule diff, the edit-preservation guard,
and never repinning the engagement - is what's under test.
"""
from app.extensions import db
from app.models import NoteLibraryNote
from app.services import reports as report_service

from . import factories as f


def _pinned_and_newer(monkeypatch, old_wording, new_wording,
                      old_standards=None, new_standards=None):
    """A pinned version and a newer one covering the same note key, with
    build_catalogue()/version_for() stubbed to the given wording."""
    pinned = f.library_version(label="pinned")
    newer = f.library_version(label="newer")
    db.session.add(NoteLibraryNote(library_version_id=pinned.id, key="revenue",
                                   heading="Revenue", standards=old_standards,
                                   sort_order=0))
    db.session.add(NoteLibraryNote(library_version_id=newer.id, key="revenue",
                                   heading="Revenue", standards=new_standards,
                                   sort_order=0))
    db.session.flush()

    catalogues = {
        pinned.id: [{"key": "revenue",
                    "pieces": [{"wording": old_wording}]}],
        newer.id: [{"key": "revenue",
                   "pieces": [{"wording": new_wording}]}],
    }
    from app.services import note_library
    monkeypatch.setattr(note_library, "build_catalogue",
                        lambda vid: catalogues[vid])
    monkeypatch.setattr(note_library, "version_for", lambda end_date: newer)
    return pinned, newer


def _section(pinned):
    rep = f.report(note_keys=("revenue",))
    section = rep.sections[0]
    fy = rep.financial_year
    fy.library_version_id = pinned.id
    db.session.flush()
    return section, fy


def test_no_alert_when_already_on_the_newest_version(db, monkeypatch):
    pinned, newer = _pinned_and_newer(monkeypatch, "Same text.", "Same text.")
    section, fy = _section(pinned)
    fy.library_version_id = newer.id
    db.session.flush()

    assert report_service.acra_alert(section, fy) is None


def test_no_alert_when_wording_is_identical_across_versions(db, monkeypatch):
    pinned, newer = _pinned_and_newer(
        monkeypatch, "Revenue is recognised at a point in time.",
        "Revenue is recognised at a point in time.")
    section, fy = _section(pinned)

    assert report_service.acra_alert(section, fy) is None


def test_alert_fires_with_a_diff_when_wording_differs(db, monkeypatch):
    pinned, newer = _pinned_and_newer(
        monkeypatch,
        "Revenue is recognised at a point in time.",
        "Revenue is recognised over time where performance obligations are "
        "satisfied over time.")
    section, fy = _section(pinned)

    alert = report_service.acra_alert(section, fy)

    assert alert is not None
    assert alert["from_version"] == pinned.version_label
    assert alert["to_version"] == newer.version_label
    assert alert["to_version_id"] == newer.id
    assert any("over" in line for line in alert["diff"])
    # F4: no claim that ACRA caused it anywhere in what is returned.
    import json
    assert "ACRA" not in json.dumps(alert)


def test_standards_label_shown_separately_when_it_differs(db, monkeypatch):
    pinned, newer = _pinned_and_newer(
        monkeypatch, "Old wording.", "New wording.",
        old_standards="FRS 115", new_standards="SFRS(I) 15")
    section, fy = _section(pinned)

    alert = report_service.acra_alert(section, fy)

    assert alert["standards_changed"] == {"before": "FRS 115",
                                          "after": "SFRS(I) 15"}


def test_adopting_new_wording_never_repins_the_engagement(db, monkeypatch):
    pinned, newer = _pinned_and_newer(monkeypatch, "Old wording.",
                                      "New wording.")
    section, fy = _section(pinned)
    section.wording_source = "library"
    section.content_html = section.library_html  # unedited
    db.session.flush()

    monkeypatch.setattr(report_service, "_render_note_content",
                        lambda note, financial_year, present=None: "New wording.")

    result = report_service.adopt_library_version(section, newer, fy)

    assert result["ok"] is True
    assert section.library_version_adopted == newer.id
    assert section.content_html == "New wording."
    # F2: the engagement's own pin is untouched.
    assert fy.library_version_id == pinned.id


def test_adopting_new_wording_refuses_to_discard_an_edit(db, monkeypatch):
    pinned, newer = _pinned_and_newer(monkeypatch, "Old wording.",
                                      "New wording.")
    section, fy = _section(pinned)
    section.wording_source = "library"
    section.library_html = "Old wording."
    section.content_html = "The preparer's own sentence."  # edited away
    db.session.flush()

    monkeypatch.setattr(report_service, "_render_note_content",
                        lambda note, financial_year, present=None: "New wording.")

    blocked = report_service.adopt_library_version(section, newer, fy)
    assert blocked["ok"] is False
    assert blocked["needs_confirm"] is True
    assert section.content_html == "The preparer's own sentence."

    forced = report_service.adopt_library_version(section, newer, fy,
                                                   force=True)
    assert forced["ok"] is True
    assert section.content_html == "New wording."
