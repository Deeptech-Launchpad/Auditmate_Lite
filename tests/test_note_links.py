"""The Note column on the Statements screen, and its links.

Covers acceptance criteria A2 and A5.
"""
from app.services.reports import note_anchor_map

from . import factories as f


def test_statements_screen_shows_the_note_column(auth_client, db):
    fy = f.financial_year()
    f.report(fy=fy, note_keys=("cash_and_cash_equivalents",))
    stmt = f.statement(fy=fy, lines=[
        ("cash_and_cash_equivalents", "Cash and cash equivalents",
         "cash_and_cash_equivalents", 77520),
    ])

    page = auth_client.get(f"/statements/{stmt.id}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert ">Note<" in body
    assert "#sec-note__cash_and_cash_equivalents" in body


def test_note_column_is_omitted_when_there_is_no_report(auth_client, db):
    """A5. Numbers are counted from the report's enabled notes, so before a
    report exists there is nothing to number - the column comes off rather
    than printing an empty one."""
    fy = f.financial_year()
    assert fy.report is None
    stmt = f.statement(fy=fy, lines=[
        ("cash_and_cash_equivalents", "Cash and cash equivalents",
         "cash_and_cash_equivalents", 77520),
    ])

    page = auth_client.get(f"/statements/{stmt.id}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert ">Note<" not in body
    assert "#sec-note__" not in body


def test_link_target_matches_the_reports_own_anchor(auth_client, db):
    """The link is only useful if it lands. The report renders each note as
    id="sec-<section_key>"; assert the href the statements screen emits is
    exactly an id the report page contains."""
    fy = f.financial_year()
    rep = f.report(fy=fy, note_keys=("revenue", "cash_and_cash_equivalents"))
    stmt = f.statement(fy=fy, lines=[
        ("cash_and_cash_equivalents", "Cash and cash equivalents",
         "cash_and_cash_equivalents", 77520),
    ])

    anchor = note_anchor_map(rep)["cash_and_cash_equivalents"]
    body = auth_client.get(f"/statements/{stmt.id}").get_data(as_text=True)
    assert f"#{anchor}" in body
    # and the anchor is the id form the document template writes
    section = next(s for s in rep.sections
                   if s.section_key == "note__cash_and_cash_equivalents")
    assert anchor == f"sec-{section.section_key}"


def test_totals_carry_no_note_number(auth_client, db):
    """A total is the statement's skeleton, not an account, so it has no
    note - the cell must be empty rather than inheriting the row above."""
    fy = f.financial_year()
    f.report(fy=fy, note_keys=("cash_and_cash_equivalents",))
    stmt = f.statement(fy=fy, lines=[
        ("cash_and_cash_equivalents", "Cash and cash equivalents",
         "cash_and_cash_equivalents", 77520),
    ])
    total = stmt.lines[0]
    total.line_key = "total_current_assets"
    total.label = "Total current assets"
    total.note_ref = None
    total.is_total = True
    db.session.flush()

    body = auth_client.get(f"/statements/{stmt.id}").get_data(as_text=True)
    assert "Total current assets" in body
    assert "#sec-note__cash_and_cash_equivalents" not in body


def test_trial_balance_has_no_note_column(auth_client, db):
    """The trial balance mirrors the client's own chart of accounts; note
    numbers have no meaning against it."""
    fy = f.financial_year()
    f.report(fy=fy, note_keys=("revenue",))
    stmt = f.statement(fy=fy, statement_type="trial_balance", lines=[
        ("tb_1", "Cash at Bank", None, 76320),
    ])
    body = auth_client.get(f"/statements/{stmt.id}").get_data(as_text=True)
    assert ">Note<" not in body
