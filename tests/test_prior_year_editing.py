"""Prior-year note table cells: editable, with their own override column.

Covers acceptance criterion D1 - a prior-year cell edit persists, records
its reason, and shows as an override, independently of the current-year
cell on the same row.
"""
from decimal import Decimal

import pytest

from app.services import overrides as overrides_service
from app.services.reports import apply_note_overrides

from . import factories as f


@pytest.fixture()
def section(db):
    rep = f.report(note_keys=("provisions",))
    return rep.sections[0]


# --- overrides.set_figure() / clear(): the previous-year column -----------

def test_previous_amount_is_stored_separately_from_current(db, section):
    overrides_service.set_figure(
        section, 0, 0, reason="Agreed to last year's signed accounts",
        previous_amount=Decimal("5000"), source_amount=Decimal("12600"),
        source_amount_previous=Decimal("4800"))
    row = overrides_service.for_row(section, 0, 0)
    assert row.amount_previous_override == Decimal("5000")
    assert row.amount_override is None
    assert row.source_amount_previous == Decimal("4800")
    assert row.source_amount == Decimal("12600")


def test_both_years_can_be_set_on_one_row(db, section):
    overrides_service.set_figure(
        section, 0, 0, reason="Correcting both years",
        amount=Decimal("13000"), previous_amount=Decimal("5000"),
        source_amount=Decimal("12600"), source_amount_previous=Decimal("4800"))
    row = overrides_service.for_row(section, 0, 0)
    assert row.amount_override == Decimal("13000")
    assert row.amount_previous_override == Decimal("5000")


def test_setting_only_previous_does_not_touch_current(db, section):
    """A row already carrying a current-year override must keep it when a
    separate request only edits the prior year."""
    overrides_service.set_figure(
        section, 0, 0, reason="This year", amount=Decimal("13000"),
        source_amount=Decimal("12600"))
    overrides_service.set_figure(
        section, 0, 0, reason="Last year", previous_amount=Decimal("5000"),
        source_amount_previous=Decimal("4800"))
    row = overrides_service.for_row(section, 0, 0)
    assert row.amount_override == Decimal("13000")
    assert row.amount_previous_override == Decimal("5000")


def test_source_amount_previous_is_recorded_once(db, section):
    """OV-03: the second edit of a row must not overwrite the record of
    what the source originally gave."""
    overrides_service.set_figure(
        section, 0, 0, reason="First edit", previous_amount=Decimal("5000"),
        source_amount_previous=Decimal("4800"))
    overrides_service.set_figure(
        section, 0, 0, reason="Second edit", previous_amount=Decimal("5500"),
        source_amount_previous=Decimal("9999"))   # must be ignored
    row = overrides_service.for_row(section, 0, 0)
    assert row.amount_previous_override == Decimal("5500")
    assert row.source_amount_previous == Decimal("4800")


def test_row_with_only_previous_set_is_not_empty(db, section):
    """is_empty (and so is_live) must recognise the new column - otherwise
    a prior-year-only edit would be treated as a stray row and deleted."""
    override = overrides_service.set_figure(
        section, 0, 0, reason="Last year", previous_amount=Decimal("5000"),
        source_amount_previous=Decimal("4800"))
    assert override is not None
    assert not override.is_empty
    assert override.is_live


def test_clearing_restores_both_years(db, section):
    overrides_service.set_figure(
        section, 0, 0, reason="Both years", amount=Decimal("13000"),
        previous_amount=Decimal("5000"), source_amount=Decimal("12600"),
        source_amount_previous=Decimal("4800"))
    row = overrides_service.for_row(section, 0, 0)
    overrides_service.clear(row)
    assert row.amount_override is None
    assert row.amount_previous_override is None
    assert not row.is_live


def test_clearing_previous_only_row_deletes_no_history(db, section):
    override = overrides_service.set_figure(
        section, 0, 0, reason="Last year", previous_amount=Decimal("5000"),
        source_amount_previous=Decimal("4800"))
    overrides_service.clear(override)
    events = [e.field for e in override.events]
    assert "previous_amount" in events


# --- apply_note_overrides(): what actually renders -------------------------

def _table(current=12600, previous=4800, label="Non-current"):
    return [{"rows": [{"label": label, "current": Decimal(str(current)),
                       "previous": Decimal(str(previous))}]}]


def test_previous_override_changes_the_rendered_previous_value(db, section):
    overrides_service.set_figure(
        section, 0, 0, reason="Agreed to last year's signed accounts",
        previous_amount=Decimal("5000"), source_amount_previous=Decimal("4800"))
    tables = _table()
    apply_note_overrides(section, tables)
    row = tables[0]["rows"][0]
    assert row["previous"] == Decimal("5000")
    assert row["computed_previous"] == Decimal("4800")
    assert row["previous_overridden"] is True
    assert row["current"] == Decimal("12600")   # untouched


def test_previous_only_override_does_not_mark_current_as_overridden(db, section):
    """The bug this change could easily have introduced: override_record is
    set on the row whenever EITHER year is overridden, so the current-year
    cell must check row.overridden specifically, not just whether a record
    exists, or it would show a false "typed" mark on a figure nobody
    touched."""
    overrides_service.set_figure(
        section, 0, 0, reason="Last year only", previous_amount=Decimal("5000"),
        source_amount_previous=Decimal("4800"))
    tables = _table()
    apply_note_overrides(section, tables)
    row = tables[0]["rows"][0]
    assert row.get("overridden") is not True
    assert row["previous_overridden"] is True
    assert "computed_current" not in row


def test_previous_override_record_carries_its_own_source_amount(db, section):
    """The mark on the prior-year cell must read against THAT year's
    source, not the current year's - the two are different facts and a
    reviewer comparing "source gave X" must see the right X."""
    overrides_service.set_figure(
        section, 0, 0, reason="r", amount=Decimal("13000"),
        previous_amount=Decimal("5000"),
        source_amount=Decimal("12600"), source_amount_previous=Decimal("4800"))
    tables = _table()
    apply_note_overrides(section, tables)
    row = tables[0]["rows"][0]
    assert row["override_record"]["source_amount"] == Decimal("12600")
    assert row["override_record_previous"]["source_amount"] == Decimal("4800")


def test_a_rebuilt_row_with_a_different_label_shows_previous_as_stale(db, section):
    """The staleness guard (anchor_label) already protected the current-year
    cell from landing on the wrong account after a rebuild; the same table
    row carries the prior-year override, so it must be covered by the same
    guard, not applied regardless of whether the row still matches."""
    overrides_service.set_figure(
        section, 0, 0, reason="r", previous_amount=Decimal("5000"),
        anchor_label="Non-current", source_amount_previous=Decimal("4800"))
    tables = _table(label="Current")   # the note was rebuilt with new rows
    apply_note_overrides(section, tables)
    row = tables[0]["rows"][0]
    assert row.get("stale_override") == "Non-current"
    assert "previous_overridden" not in row
    assert row["previous"] == Decimal("4800")   # untouched, not the override


# --- the endpoint: revert detection across two independent fields --------

@pytest.fixture()
def source_row(monkeypatch):
    """_source_row() needs a real client template and trial balance to
    build from; that machinery is not what this test is about, so the
    source figures are supplied directly."""
    import app.blueprints.reports as reports_bp

    values = {"label": "Non-current", "current": Decimal("12600"),
             "previous": Decimal("4800"), "source_name": "the trial balance"}
    monkeypatch.setattr(reports_bp, "_source_row", lambda *a, **k: values)
    return values


def test_reverting_previous_alone_clears_only_that_field(
        auth_client, db, section, source_row):
    overrides_service.set_figure(
        section, 0, 0, reason="Last year", previous_amount=Decimal("5000"),
        source_amount_previous=Decimal("4800"))

    response = auth_client.patch("/reports/api/note-row", json={
        "section_id": section.id, "table_index": 0, "row_index": 0,
        "previous": "4,800", "reason": "back to source"})
    data = response.get_json()
    assert data["ok"] is True
    assert data["cleared"] is True
    assert float(data["previous"]) == 4800.0

    row = overrides_service.for_row(section, 0, 0)
    assert row is None or not row.is_live


def test_editing_previous_requires_a_reason(auth_client, db, section, source_row):
    response = auth_client.patch("/reports/api/note-row", json={
        "section_id": section.id, "table_index": 0, "row_index": 0,
        "previous": "5,000"})
    assert response.status_code == 400
    assert response.get_json()["needs_reason"] is True


def test_editing_previous_persists_with_a_reason(auth_client, db, section, source_row):
    response = auth_client.patch("/reports/api/note-row", json={
        "section_id": section.id, "table_index": 0, "row_index": 0,
        "previous": "5,000", "reason": "agreed to the signed accounts"})
    data = response.get_json()
    assert data["ok"] is True
    assert float(data["source_amount_previous"]) == 4800.0

    row = overrides_service.for_row(section, 0, 0)
    assert row.amount_previous_override == Decimal("5000")
    assert row.reason == "agreed to the signed accounts"
