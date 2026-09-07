"""One-time fix: bring the note_library_entries row for "other_income" in
line with the corrected config/notes_catalogue.yaml.

seed-note-library only ever runs once (it refuses if the table already has
rows - see app/cli.py), so the YAML fix alone does not reach a database that
was already seeded. This applies the same correction directly: removes
'trade_receivables' as a trigger for the whole note, and turns piece F107-06
from an auto-populated table (wrongly pulling a receivables BALANCE into a
row headed "gains or losses") into a manual entry - there is no gains/losses
figure computable from a plain trial balance, so it should never have been
auto-built in the first place.

Only this one row is touched, matched by key, and only the two fields that
changed. Everything else about the note - its heading, its other pieces,
any auditor edits already made in reports built from it - is untouched.

Run with DRY_RUN=True (the default) first. Rerun with DRY_RUN=False to apply.
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, os.getcwd())

import yaml

from app import create_app, db
from app.models import NoteLibraryEntry

DRY_RUN = True


def main():
    app = create_app()
    with app.app_context():
        yaml_path = Path(app.config["CONFIG_DIR"]) / "notes_catalogue.yaml"
        catalogue = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or []
        corrected = next((n for n in catalogue if n["key"] == "other_income"), None)
        if corrected is None:
            print("other_income not found in notes_catalogue.yaml - aborting.")
            return

        row = NoteLibraryEntry.query.filter_by(key="other_income").first()
        if row is None:
            print("No note_library_entries row for key=other_income - nothing to fix.")
            return

        print("BEFORE:")
        print("  trigger_keys:", row.trigger_keys)
        print("  pieces:", row.pieces)
        print("\nAFTER (from corrected YAML):")
        print("  trigger_keys:", corrected.get("trigger_keys"))
        print("  pieces:", corrected.get("pieces"))

        if DRY_RUN:
            print("\nDry run only - nothing written. Rerun with DRY_RUN=False "
                  "to apply.")
            return

        row.trigger_keys = corrected.get("trigger_keys")
        row.pieces = corrected.get("pieces") or []
        db.session.commit()
        print("\nDone - note_library_entries row for other_income updated.")


if __name__ == "__main__":
    main()
