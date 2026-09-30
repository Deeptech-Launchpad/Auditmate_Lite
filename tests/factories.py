"""Minimal engagements, built in code.

Deliberately not the `seed_*` services: those build a realistic client with
documents and extractions, which is the right thing for a demo and far too
much for a test that wants two notes and a statement line. These build only
what the assertion needs.
"""
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models import (AuditReport, AuditReportSection, Customer,
                        FinancialStatement, FinancialYear, NoteLibraryNote,
                        NoteLibrarySheet, NoteLibraryVersion, StatementLine)


def customer(name="Testco Pte Ltd"):
    row = Customer(name=name, uen="201900000X")
    db.session.add(row)
    db.session.flush()
    return row


def financial_year(cust=None, label="FY2025", approved=True):
    cust = cust or customer()
    fy = FinancialYear(
        customer_id=cust.id,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        year_label=label,
        tb_status="approved" if approved else "draft",
    )
    db.session.add(fy)
    db.session.flush()
    return fy


def report(fy=None, note_keys=("revenue", "cash_and_cash_equivalents")):
    """A report carrying `note_keys` as enabled notes, in the order given."""
    fy = fy or financial_year()
    rep = AuditReport(financial_year_id=fy.id, status="draft")
    db.session.add(rep)
    db.session.flush()
    for order, key in enumerate(note_keys):
        db.session.add(AuditReportSection(
            report_id=rep.id,
            section_key=f"note__{key}",
            title=key.replace("_", " ").title(),
            section_type="free_text",
            sort_order=order,
            is_enabled=True,
        ))
    db.session.flush()
    return rep


def library_version(notes=(), preparer_inputs=(), label=None):
    """A minimal notes-library version, self-contained for a test.

    `auditmate_test` starts empty except for schema - it is never seeded
    with a real workbook import, so a test that queries
    `NoteLibraryVersion.query.get(<id>)` against whatever happens to be
    imported in a developer's own database is not testing anything
    reliable. This builds only what a test actually needs: a handful of
    notes (key, code, heading) and a "Preparer inputs" sheet of raw rows,
    the same shape `version.sheet("Preparer inputs")` returns from a real
    import.

    `notes` is [(key, library_code, heading)]. `preparer_inputs` is
    [{"Item": ..., "Note": ..., ...}] - whatever columns the row needs.
    """
    import uuid
    from datetime import date

    version = NoteLibraryVersion(
        version_label=label or f"test-{uuid.uuid4().hex[:12]}",
        valid_from=date(2020, 1, 1), valid_to=date(2099, 12, 31),
        status="active")
    db.session.add(version)
    db.session.flush()

    for order, (key, code, heading) in enumerate(notes):
        db.session.add(NoteLibraryNote(
            library_version_id=version.id, key=key, library_code=code,
            heading=heading, sort_order=order))

    if preparer_inputs:
        db.session.add(NoteLibrarySheet(
            library_version_id=version.id, name="Preparer inputs",
            rows=list(preparer_inputs), row_count=len(preparer_inputs)))

    db.session.flush()
    return version


def statement(fy=None, statement_type="balance_sheet", lines=()):
    """`lines` is a sequence of (line_key, label, note_ref, amount)."""
    fy = fy or financial_year()
    stmt = FinancialStatement(financial_year_id=fy.id,
                              statement_type=statement_type)
    db.session.add(stmt)
    db.session.flush()
    for order, (line_key, label, note_ref, amount) in enumerate(lines):
        db.session.add(StatementLine(
            statement_id=stmt.id,
            line_key=line_key,
            label=label,
            group_key="current_assets",
            sort_order=order,
            note_ref=note_ref,
            amount_current=Decimal(str(amount)) if amount is not None else None,
        ))
    db.session.flush()
    return stmt
