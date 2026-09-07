"""One-time fix: bring existing "Other income" report sections in line with
the corrected note_library_entries row (see fix_other_income_trigger.py).

That fix only changes what a NEWLY created report does - an existing
section's table definition was already baked into its data_binding at the
point the report was first built, back when the note wrongly triggered off
trade_receivables and one of its pieces pulled that BALANCE into a table
headed "net gains or losses". Fixing the library does not reach back and
recompute a section that already exists.

For each "note__other_income" section that is STILL exactly the untouched
placeholder (never touches a section an auditor has written into):
  - recompute whether the note should trigger at all under the corrected
    rule (a real Other Income account, this year or last - not receivables)
  - if it should not trigger: switch the section off. Nothing is lost -
    it never held real content, and the numbering elsewhere in the report
    is already recalculated from whichever notes are enabled, so turning
    this off just stops it printing an empty, wrongly-triggered heading.
  - if it should still trigger (a genuine Other Income account exists):
    rebuild its content and table from the corrected library definition,
    same as a fresh report would get today.

Run with DRY_RUN=True (the default) first. Rerun with DRY_RUN=False to apply.
"""
import sys
import os

sys.path.insert(0, os.getcwd())

from app import create_app, db
from app.models import AuditReportSection, FinancialYear, Customer
from app.services.reports import (
    UNWRITTEN_NOTE_FORMS, load_notes_catalogue, _present_keys,
    _note_triggered, _assemble_note_content,
)

DRY_RUN = True


def main():
    app = create_app()
    with app.app_context():
        catalogue = {n["key"]: n for n in load_notes_catalogue()}
        note = catalogue.get("other_income")
        if note is None:
            print("other_income not found in the note library - aborting.")
            return

        sections = (AuditReportSection.query
                    .filter_by(section_key="note__other_income")
                    .all())

        turned_off = []
        rebuilt = []
        skipped_edited = []

        for section in sections:
            body = (section.content_html or "").strip()
            report = section.report
            fy = db.session.get(FinancialYear, report.financial_year_id)
            customer = db.session.get(Customer, fy.customer_id)
            label = f"{customer.name} / {fy.year_label}"

            if body not in UNWRITTEN_NOTE_FORMS:
                if section.is_enabled:
                    skipped_edited.append(label)
                continue

            present = _present_keys(fy)
            should_trigger = _note_triggered(note, present)

            if not should_trigger:
                if section.is_enabled:
                    turned_off.append(label)
                    if not DRY_RUN:
                        section.is_enabled = False
                continue

            html, table_specs = _assemble_note_content(note, present)
            rebuilt.append(label)
            if not DRY_RUN:
                section.content_html = html
                section.data_binding = ({"note_table_specs": table_specs}
                                        if table_specs else None)

        print(f"{'DRY RUN' if DRY_RUN else 'APPLYING'}\n")
        print(f"Would switch OFF ({len(turned_off)}) - no genuine Other "
              f"Income account, note should never have triggered:")
        for label in turned_off:
            print(f"  {label}")

        print(f"\nWould REBUILD ({len(rebuilt)}) - genuine Other Income "
              f"account exists, content rebuilt from the corrected library:")
        for label in rebuilt:
            print(f"  {label}")

        if skipped_edited:
            print(f"\nLeft untouched - already has real auditor-written "
                  f"content ({len(skipped_edited)}):")
            for label in skipped_edited:
                print(f"  {label}")

        if DRY_RUN:
            print("\nDry run only - nothing written. Rerun with DRY_RUN=False "
                  "to apply.")
        else:
            db.session.commit()
            print(f"\nDone - {len(turned_off)} switched off, "
                  f"{len(rebuilt)} rebuilt.")


if __name__ == "__main__":
    main()
