"""Note numbering and the anchors that go with it.

Covers acceptance criteria A1, A3 and A4.
"""
import yaml

from app.services.reports import note_anchor_map, note_number_map

from . import factories as f


def test_numbers_follow_enabled_notes_in_order(db):
    rep = f.report(note_keys=("revenue", "finance_costs", "cash_and_cash_equivalents"))
    numbers = note_number_map(rep)
    assert numbers["revenue"] == "1"
    assert numbers["finance_costs"] == "2"
    assert numbers["cash_and_cash_equivalents"] == "3"


def test_keys_resolve_bare_and_prefixed(db):
    """A statement line carries the bare key; the rendered section carries
    the prefixed one. Both have to resolve or one of the two surfaces
    silently shows nothing."""
    rep = f.report(note_keys=("revenue",))
    numbers = note_number_map(rep)
    assert numbers["revenue"] == numbers["note__revenue"] == "1"


def test_unticking_a_note_leaves_no_gap(db):
    """A4. The number is recomputed, not stored, precisely so that
    disabling note 2 renumbers what followed it instead of printing 1, 3."""
    rep = f.report(note_keys=("revenue", "finance_costs", "cash_and_cash_equivalents"))
    assert note_number_map(rep)["cash_and_cash_equivalents"] == "3"

    middle = next(s for s in rep.sections if s.section_key == "note__finance_costs")
    middle.is_enabled = False
    db.session.flush()

    numbers = note_number_map(rep)
    assert numbers["revenue"] == "1"
    assert numbers["cash_and_cash_equivalents"] == "2"
    assert "finance_costs" not in numbers
    assert sorted(set(numbers.values())) == ["1", "2"]


def test_anchor_map_matches_the_rendered_section_id(db):
    """A2's target. The document renders id="sec-<section_key>", so the
    anchor must be exactly that, for both key forms."""
    rep = f.report(note_keys=("revenue",))
    anchors = note_anchor_map(rep)
    section = next(s for s in rep.sections if s.section_key == "note__revenue")
    assert anchors["revenue"] == f"sec-{section.section_key}"
    assert anchors["note__revenue"] == anchors["revenue"]


def test_anchor_and_number_maps_agree_on_keys(db):
    rep = f.report(note_keys=("revenue", "cash_and_cash_equivalents"))
    assert set(note_anchor_map(rep)) == set(note_number_map(rep))


def test_disabled_notes_have_no_anchor(db):
    rep = f.report(note_keys=("revenue", "finance_costs"))
    off = next(s for s in rep.sections if s.section_key == "note__finance_costs")
    off.is_enabled = False
    db.session.flush()
    assert "finance_costs" not in note_anchor_map(rep)


def test_statement_line_note_refs_are_keys_not_numbers(app):
    """A3. The cash flow carried `note: 9` - a printed number where every
    other line carries a note key - so it resolved against nothing and
    printed blank. Guard the whole config against the same mistake."""
    path = app.config["CONFIG_DIR"] / "statement_templates.yaml"
    templates = yaml.safe_load(path.read_text(encoding="utf-8"))

    offenders = []
    for statement_type, block in templates.items():
        for line in block.get("lines") or []:
            note = line.get("note")
            if note is None:
                continue
            if not isinstance(note, str) or not note.strip() or note.strip().isdigit():
                offenders.append((statement_type, line.get("key"), note))
    assert offenders == []


def test_cash_flow_closing_cash_points_at_the_cash_note(app):
    path = app.config["CONFIG_DIR"] / "statement_templates.yaml"
    templates = yaml.safe_load(path.read_text(encoding="utf-8"))
    for statement_type in ("cash_flow", "cash_flow_direct"):
        lines = {l["key"]: l for l in templates[statement_type]["lines"]}
        assert lines["cf_closing_cash"]["note"] == "cash_and_cash_equivalents"


def test_note_ref_resolves_for_a_real_statement_line(db):
    """A1 end to end: a line whose note_ref names an enabled note gets a
    number; a line pointing at a note that is not on returns nothing."""
    fy = f.financial_year()
    rep = f.report(fy=fy, note_keys=("cash_and_cash_equivalents",))
    stmt = f.statement(fy=fy, lines=[
        ("cash_and_cash_equivalents", "Cash and cash equivalents",
         "cash_and_cash_equivalents", 123429.72),
        ("inventories", "Inventories", "inventories", 0),
    ])
    numbers = note_number_map(rep)
    by_key = {l.line_key: l for l in stmt.lines}
    assert numbers.get(by_key["cash_and_cash_equivalents"].note_ref) == "1"
    assert numbers.get(by_key["inventories"].note_ref, "") == ""
