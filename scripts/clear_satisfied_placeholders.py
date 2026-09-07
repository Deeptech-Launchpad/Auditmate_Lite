"""One-time cleanup: clear "Write this note here." wherever a table already
answers the note, on reports that already existed before the fix in
app/services/reports.py (_assemble_note_content).

New reports no longer get the placeholder in this situation - see that
function. This script brings reports created before the fix in line with
what a fresh one would look like now, and nothing else: it only clears a
section whose content is *still* the exact, untouched placeholder text
(never a section an auditor has written into), and only when a real table
- one that actually has rows right now - already covers the requirement.

Run with DRY_RUN=True (the default) first and read the output. Only rerun
with DRY_RUN=False once that list looks right.
"""
import sys
import os

sys.path.insert(0, os.getcwd())

from app import create_app, db
from app.models import AuditReportSection, FinancialYear, Customer
from app.services.reports import UNWRITTEN_NOTE_FORMS
from app.services import notes as notes_service

DRY_RUN = True


def main():
    app = create_app()
    with app.app_context():
        candidates = (
            AuditReportSection.query
            .filter(AuditReportSection.is_enabled.is_(True))
            .filter(AuditReportSection.section_type != "statement")
            .all()
        )

        cleared = []
        for section in candidates:
            # "Other income" is excluded on purpose: one of its pieces pulls
            # a trade receivables BALANCE into a table headed "net gains or
            # losses" - a balance is not a gain or loss, so that table is
            # wrong, not just unwritten. See the FRS library fix this needs
            # first; clearing this note here would surface that wrong table.
            if section.section_key == "note__other_income":
                continue

            body = (section.content_html or "").strip()
            if body not in UNWRITTEN_NOTE_FORMS:
                continue

            note_table_spec = (section.data_binding or {}).get("note_table_specs")
            if not note_table_spec:
                continue

            report = section.report
            fy = db.session.get(FinancialYear, report.financial_year_id)
            tables = notes_service.build_tables(note_table_spec, fy)
            if not tables:
                continue

            customer = db.session.get(Customer, fy.customer_id)
            cleared.append((customer.name, fy.year_label, section.title, section))

        if not cleared:
            print("Nothing to clear - no report has this combination right now.")
            return

        print(f"{'DRY RUN - would clear' if DRY_RUN else 'CLEARING'} "
              f"{len(cleared)} section(s):\n")
        for customer_name, year_label, title, section in cleared:
            print(f"  {customer_name} / {year_label} / {title}")
            if not DRY_RUN:
                section.content_html = ""

        if DRY_RUN:
            print("\nDry run only - nothing written. Rerun with DRY_RUN=False "
                  "to apply.")
        else:
            db.session.commit()
            print(f"\nDone - {len(cleared)} section(s) cleared and committed.")


if __name__ == "__main__":
    main()
