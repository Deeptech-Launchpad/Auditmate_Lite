"""The versioned FRS notes library: reading a library workbook, and deciding
which version an engagement reports under.

Why versions exist at all: the library is issued per financial year end.
Version 1.0 covers year ends to 31 December 2026; FRS 118 forces a second
version for periods beginning 1 January 2027. An FY2026 and an FY2027
engagement can be open in the same week, so both versions have to be loaded
at once and an engagement has to stay on the one in force for its own period.

Nothing here writes without being asked twice: `plan()` reads the workbook and
reports what would change, `apply_plan()` is what actually writes. The CLI
defaults to the first.
"""
import hashlib
import logging
import os
import re
from datetime import date

from ..extensions import db
from ..models import (FinancialYear, NoteLibraryEntry, NoteLibraryNote,
                      NoteLibraryVersion)

log = logging.getLogger(__name__)

# The workbook puts a banner in row 1 and a description in row 2; the real
# header is row 3 on every data sheet.
HEADER_ROW = 3

REQUIRED_SHEETS = ("Version", "Notes", "Paragraphs", "Tables", "Requirements")

TICK_STATES = {
    "always on": "always",
    "tb-driven": "tb_driven",
    "manual": "manual",
}

# A paragraph's condition source decides when it prints. Until Stage 3 builds
# the real evaluator, anything that cannot be tested yet is imported as
# `manual` - offered to the preparer rather than printed. Erring the other way
# would print a note for a balance the client has not got, which is the exact
# failure this library was revised to stop.
CONDITION_TICK = {
    "unconditional": "always",
    "line balance": "tb_driven",
    "table binding": "tb_driven",
    "preparer confirms": "manual",
    "client record": "manual",
    "firm setting": "manual",
}

TABLE_TAGS = {"TABLE"}

# Headings that changed name between the August index and this library.
# Written out rather than left to fuzzy matching: a wrong match here would
# silently attach last year's wording to a different note.
RENAMES = {
    "trade receivables": "trade and other receivables",
}


def normalise_heading(text):
    """Fold a note heading to something comparable across sources."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Version selection
# ---------------------------------------------------------------------------

def version_for(year_end):
    """The library version in force for a period ending on this date.

    Returns None rather than guessing when no loaded version covers the date.
    A period reported under a library that was not in force for it is worse
    than a period that reports it has no library.
    """
    if year_end is None:
        return None
    return (NoteLibraryVersion.query
            .filter(NoteLibraryVersion.status != "draft")
            .filter(NoteLibraryVersion.valid_from <= year_end)
            .filter(NoteLibraryVersion.valid_to >= year_end)
            .order_by(NoteLibraryVersion.valid_from.desc())
            .first())


def pin_version(financial_year, commit=False):
    """Attach an engagement to its library version, once.

    An engagement that already carries a pin is left alone. That is the whole
    point: a later library must not reach backwards into a period that has
    already been reported on.
    """
    if financial_year.library_version_id:
        return financial_year.library_version_id

    version = version_for(financial_year.end_date)
    if version is None:
        return None

    financial_year.library_version_id = version.id
    if commit:
        db.session.commit()
    return version.id


def backfill_pins():
    """Pin engagements that predate versioning. Returns (pinned, unmatched)."""
    pinned = unmatched = 0
    for fy in FinancialYear.query.filter(
            FinancialYear.library_version_id.is_(None)).all():
        if pin_version(fy):
            pinned += 1
        else:
            unmatched += 1
    db.session.commit()
    return pinned, unmatched


# ---------------------------------------------------------------------------
# Reading the workbook
# ---------------------------------------------------------------------------

def _sheet_rows(ws):
    """Data rows of a sheet, with the row-3 header as a name to index map."""
    raw = list(ws.iter_rows(values_only=True))
    if len(raw) < HEADER_ROW:
        return {}, []
    header = [str(c).strip() if c is not None else "" for c in raw[HEADER_ROW - 1]]
    index = {}
    for i, name in enumerate(header):
        # Two columns on the Notes sheet share the title "Note code". The
        # first is column B, which every other sheet joins on, so it wins.
        if name and name not in index:
            index[name] = i
    body = [r for r in raw[HEADER_ROW:]
            if any(c is not None and str(c).strip() for c in r)]
    return index, body


def _second_index(ws, name):
    """Index of the *second* column carrying this header, or None.

    Column K on the Notes sheet is the NOTE_/POL_ alias. Carried for
    traceability; nothing joins on it.
    """
    raw = list(ws.iter_rows(min_row=HEADER_ROW, max_row=HEADER_ROW,
                            values_only=True))
    if not raw:
        return None
    header = [str(c).strip() if c is not None else "" for c in raw[0]]
    hits = [i for i, h in enumerate(header) if h == name]
    return hits[1] if len(hits) > 1 else None


def _cell(row, index, name, default=None):
    i = index.get(name)
    if i is None or i >= len(row):
        return default
    value = row[i]
    if value is None:
        return default
    value = str(value).strip()
    return value or default


def _split_list(text, pipe=False):
    """Split a cell holding several values. Dashes mean 'none'."""
    if not text:
        return []
    parts = re.split(r"\|", str(text)) if pipe else re.split(r"[;,\n]", str(text))
    return [p.strip() for p in parts if p and p.strip() and p.strip() != "-"]


def _int_or_none(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


MONTHS = ("january february march april may june july august september "
          "october november december").split()


def _parse_validity(text):
    """Pull the two dates out of the Version sheet prose.

    e.g. "On or after 1 January 2024 and on or before 31 December 2026".
    """
    found = []
    for match in re.finditer(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", str(text or "")):
        day, month, year = match.group(1), match.group(2).lower(), match.group(3)
        if month in MONTHS:
            found.append(date(int(year), MONTHS.index(month) + 1, int(day)))
    if len(found) >= 2:
        return found[0], found[1]
    return None, None


def read_workbook(path):
    """Parse a library workbook into plain dicts. No database access."""
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        missing = [s for s in REQUIRED_SHEETS if s not in wb.sheetnames]
        if missing:
            raise ValueError(
                "This does not look like a notes library workbook. Missing "
                "sheet(s): " + ", ".join(missing))

        # --- Version -------------------------------------------------------
        vi, vrows = _sheet_rows(wb["Version"])
        meta = {}
        for row in vrows:
            item = _cell(row, vi, "Item")
            if item:
                meta[item.lower()] = _cell(row, vi, "Value", "")
        valid_from, valid_to = _parse_validity(
            meta.get("valid for financial years ending", ""))
        version = {
            "version_label": meta.get("version") or "unknown",
            "framework": meta.get("framework"),
            "entity_scope": meta.get("entity scope"),
            "valid_from": valid_from,
            "valid_to": valid_to,
        }

        # --- Requirements: ref to requirement text and TB mapping ----------
        ri, rrows = _sheet_rows(wb["Requirements"])
        requirements, mappings = {}, {}
        for row in rrows:
            ref = _cell(row, ri, "Ref")
            if not ref:
                continue
            requirements[ref] = _cell(row, ri, "Disclosure requirement (paraphrased)")
            mappings[ref] = _cell(row, ri, "TB / FS mapping")

        # --- Paragraphs, grouped by note -----------------------------------
        pi, prows = _sheet_rows(wb["Paragraphs"])
        paragraphs = {}
        for row in prows:
            code = _cell(row, pi, "Note code")
            if not code:
                continue
            tag = (_cell(row, pi, "Tag") or "").upper()
            source = (_cell(row, pi, "Source") or "").lower()
            condition_source = (_cell(row, pi, "Condition source") or "").lower()
            ref = _cell(row, pi, "Index ref")
            paragraphs.setdefault(code, []).append({
                "para_id": _cell(row, pi, "Para ID"),
                "sort_order": _int_or_none(_cell(row, pi, "sort_order")) or 0,
                "ref": ref,
                "output_form": ("Table" if tag in TABLE_TAGS
                                else "Narrative paragraph"),
                "tick_state": CONDITION_TICK.get(condition_source, "manual"),
                "wording": _cell(row, pi, "Text", ""),
                "build_note": mappings.get(ref) or "",
                "requirement": requirements.get(ref) or "",
                # Stage 3 fills tb_keys from config/line_code_map.yaml. Until
                # it does, a figure-bound paragraph has nothing to resolve
                # against, so it stays off rather than printing blank.
                "tb_keys": [],
                "tag": tag,
                "condition_text": _cell(row, pi, "Condition"),
                "condition_source": _cell(row, pi, "Condition source"),
                "line_codes": _split_list(_cell(row, pi, "Line codes")),
                "binds_to": _cell(row, pi, "Binds to"),
                # 68 paragraphs are drafted rather than taken from the index
                # and must not reach a client document before a reviewer has
                # seen them. Recorded per paragraph so the gate is real.
                "review_status": ("unreviewed" if source.startswith("drafted")
                                  else "from_index"),
            })

        # --- Tables, grouped by note ---------------------------------------
        ti, trows = _sheet_rows(wb["Tables"])
        tables = {}
        for row in trows:
            code = _cell(row, ti, "Note code")
            if not code:
                continue
            tables.setdefault(code, []).append({
                "table_id": _cell(row, ti, "Table ID"),
                "sort_order": _int_or_none(_cell(row, ti, "sort_order")) or 0,
                "ref": _cell(row, ti, "Index ref"),
                "row_labels": _split_list(_cell(row, ti, "Row labels"), pipe=True),
                "column_labels": _cell(row, ti, "Column labels"),
                "periods": _cell(row, ti, "Periods presented"),
                "comparative": _cell(row, ti, "Comparative"),
                "total_row": _cell(row, ti, "Total row"),
                "build_note": _cell(row, ti, "Build note"),
                "line_codes": _split_list(_cell(row, ti, "Line codes")),
                "cross_references": _cell(row, ti, "Cross-references"),
            })

        # --- Notes ---------------------------------------------------------
        ni, nrows = _sheet_rows(wb["Notes"])
        alt_index = _second_index(wb["Notes"], "Note code")
        notes = []
        for row in nrows:
            code = _cell(row, ni, "Note code")
            heading = _cell(row, ni, "Note heading")
            if not code or not heading:
                continue
            alt = None
            if alt_index is not None and alt_index < len(row) and row[alt_index]:
                alt = str(row[alt_index]).strip()
            notes.append({
                "library_code": code,
                "library_code_alt": alt,
                "heading": heading,
                "tick_state": TICK_STATES.get(
                    (_cell(row, ni, "Default tick state") or "").lower(), "manual"),
                "sort_order": _int_or_none(_cell(row, ni, "#")) or 0,
                "section_no": _int_or_none(_cell(row, ni, "Section")),
                "section_name": _cell(row, ni, "Section name"),
                "standards": _cell(row, ni, "Standards"),
                "presented_as": _cell(row, ni, "Presented as"),
                "sits_inside": _cell(row, ni, "Sits inside"),
                "trigger_text": _cell(row, ni, "Trigger"),
                # Stage 3 converts trigger_text into real keys. Empty until
                # then, so a TB-driven note stays off rather than firing on
                # nothing at all.
                "trigger_keys": [],
                "pieces": sorted(paragraphs.get(code, []),
                                 key=lambda p: p["sort_order"]),
                "tables": sorted(tables.get(code, []),
                                 key=lambda t: t["sort_order"]),
            })

        known = {n["library_code"] for n in notes}
        return {
            "version": version,
            "notes": notes,
            "orphan_paragraphs": sorted(set(paragraphs) - known),
            "orphan_tables": sorted(set(tables) - known),
        }
    finally:
        wb.close()


# ---------------------------------------------------------------------------
# Planning and applying an import
# ---------------------------------------------------------------------------

def plan(path):
    """Read a workbook and work out what importing it would do.

    Touches the database only to read. Returns (report, data) where `data` is
    what `apply_plan` acts on, so what is shown and what is written cannot
    drift apart.
    """
    data = read_workbook(path)
    digest = file_digest(path)
    version = data["version"]

    existing = NoteLibraryVersion.query.filter_by(
        version_label=version["version_label"]).first()
    same_file = NoteLibraryVersion.query.filter_by(source_sha256=digest).first()

    # Notes already held, folded by heading, so a note we have keeps the key
    # the report engine already references - including from reports that have
    # been issued and frozen.
    current = {normalise_heading(row.heading): row
               for row in NoteLibraryEntry.query.all()}

    matched, added = [], []
    for note in data["notes"]:
        folded = normalise_heading(note["heading"])
        hit = current.get(folded)
        if hit is None:
            for old, new in RENAMES.items():
                if normalise_heading(new) == folded:
                    hit = current.get(normalise_heading(old))
                    break
        if hit is not None:
            note["key"] = hit.key
            matched.append(note)
        else:
            note["key"] = note["library_code"]
            added.append(note)

    unreviewed = sum(1 for n in data["notes"] for p in n["pieces"]
                     if p["review_status"] == "unreviewed")
    line_codes = {c for n in data["notes"] for p in n["pieces"]
                  for c in p["line_codes"]}
    line_codes |= {c for n in data["notes"] for t in n["tables"]
                   for c in t["line_codes"]}

    report = {
        "version_label": version["version_label"],
        "framework": version["framework"],
        "valid_from": version["valid_from"],
        "valid_to": version["valid_to"],
        "digest": digest,
        "already_loaded": existing is not None,
        "same_file_loaded_as": same_file.version_label if same_file else None,
        "total_notes": len(data["notes"]),
        "matched": len(matched),
        "added": len(added),
        "added_headings": [n["heading"] for n in added],
        "paragraphs": sum(len(n["pieces"]) for n in data["notes"]),
        "tables": sum(len(n["tables"]) for n in data["notes"]),
        "unreviewed_paragraphs": unreviewed,
        "auditor_notes_untouched": NoteLibraryEntry.query.filter_by(
            source="auditor_added").count(),
        "orphan_paragraphs": data["orphan_paragraphs"],
        "orphan_tables": data["orphan_tables"],
        "distinct_line_codes": len(line_codes),
        "notes_always_on": sum(1 for n in data["notes"]
                               if n["tick_state"] == "always"),
        "notes_tb_driven": sum(1 for n in data["notes"]
                               if n["tick_state"] == "tb_driven"),
        "notes_manual": sum(1 for n in data["notes"]
                            if n["tick_state"] == "manual"),
    }
    return report, data


def _pieces_for(note):
    """Paragraph pieces plus one piece per figure table, in document order."""
    pieces = list(note["pieces"])
    for table in note["tables"]:
        pieces.append({
            "ref": table["ref"],
            "output_form": "Table",
            "tick_state": "tb_driven",
            "wording": "",
            "build_note": table["build_note"] or "",
            "requirement": "",
            "tb_keys": [],
            "tag": "TABLE",
            "table_id": table["table_id"],
            "row_labels": table["row_labels"],
            "column_labels": table["column_labels"],
            "periods": table["periods"],
            "total_row": table["total_row"],
            "line_codes": table["line_codes"],
            "cross_references": table["cross_references"],
            "review_status": "from_index",
        })
    return pieces


def apply_plan(data, digest, path, activate=False, user_id=None):
    """Write a planned import.

    Purely additive: a new version row and its own notes. Nothing already in
    the database is modified, so an import can never disturb an engagement
    already pinned to an earlier version, or a note an auditor wrote.
    """
    version = data["version"]
    if not version["valid_from"] or not version["valid_to"]:
        raise ValueError(
            "The Version sheet does not state which financial year ends this "
            "library is valid for, so no engagement could be matched to it. "
            "Refusing to import.")

    row = NoteLibraryVersion(
        version_label=version["version_label"],
        framework=version["framework"],
        entity_scope=version["entity_scope"],
        valid_from=version["valid_from"],
        valid_to=version["valid_to"],
        source_filename=os.path.basename(str(path)),
        source_sha256=digest,
        imported_by=user_id,
        status="active" if activate else "draft",
        notes_count=len(data["notes"]),
    )
    db.session.add(row)
    db.session.flush()

    for note in data["notes"]:
        db.session.add(NoteLibraryNote(
            library_version_id=row.id,
            key=note["key"],
            library_code=note["library_code"],
            library_code_alt=note["library_code_alt"],
            heading=note["heading"],
            tick_state=note["tick_state"],
            sort_order=note["sort_order"],
            trigger_keys=note["trigger_keys"],
            pieces=_pieces_for(note),
            subsections=[],
            section_no=note["section_no"],
            section_name=note["section_name"],
            standards=note["standards"],
            presented_as=note["presented_as"],
            sits_inside=note["sits_inside"],
            trigger_text=note["trigger_text"],
        ))

    db.session.commit()
    log.info("Imported notes library %s (%d notes)",
             row.version_label, len(data["notes"]))
    return row


# ---------------------------------------------------------------------------
# Reading a version back out: nesting, and what makes a note fire
# ---------------------------------------------------------------------------


def load_line_code_map():
    """The library's line codes against this system's standard keys.

    Read fresh rather than cached at import time so an edit to the YAML takes
    effect on the next report build, the same way `load_notes_catalogue`
    deliberately re-reads the library table.
    """
    import yaml
    from flask import current_app

    path = current_app.config["CONFIG_DIR"] / "line_code_map.yaml"
    if not path.exists():
        log.warning("config/line_code_map.yaml is missing - every TB-driven "
                    "note will fall back to manual")
        return {}, set()
    with open(path, "r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    return (doc.get("codes") or {}), set(doc.get("non_figure") or [])


def _codes_of(piece):
    return [c for c in (piece.get("line_codes") or []) if c]


def resolve_keys(codes, code_map):
    """Standard keys for a set of library line codes, in a stable order."""
    keys = []
    for code in codes:
        for key in (code_map.get(code) or []):
            if key not in keys:
                keys.append(key)
    return keys


def _resolve_pieces(pieces, code_map, non_figure):
    """Fill each piece's tb_keys from its line codes.

    A piece the library marked TB-driven whose codes resolve to no key at all
    cannot be tested, so it is switched to manual rather than left looking
    automatic and never firing. Narrative codes (STATIC, CLIENT and the rest)
    are exempt: they were never about a balance in the first place.
    """
    out = []
    for piece in pieces:
        piece = dict(piece)
        codes = _codes_of(piece)
        piece["tb_keys"] = resolve_keys(codes, code_map)

        if (piece.get("tick_state") == "tb_driven"
                and not piece["tb_keys"]
                and not any(c in non_figure for c in codes)):
            piece["tick_state"] = "manual"
            piece["downgraded"] = True
        out.append(piece)
    return out


def _note_dict(row, code_map, non_figure, inherited):
    """One library note in the shape `services/reports.py` reads.

    `inherited` is the flat catalogue's row for this note, or None. Where
    there is one, TWO things carry over from it.

    ITS TRIGGER KEYS WIN over anything derived from line codes. Those
    triggers are in production today, hand-checked, and deciding notes for
    live engagements. A new mapping is allowed to fill a gap; it is not
    allowed to quietly change a note that already works.

    AND A NOTE THAT APPEARS BY ITSELF TODAY GOES ON DOING SO. The new library
    is stricter about several notes - it says, rightly, that a trial balance
    cannot tell you whether assets have been pledged as security, so that one
    is a manual confirmation. Rightly or not, adopting that here would mean a
    note some engagements print this week silently stops appearing, and the
    preparer is never told. A note switched on that should not be is visible
    and takes one click to remove; a note missing from a set of accounts is
    neither. So the more inclusive of the two states is kept, and the note
    records that it was, for the changeover report to list.

    The library's own stricter state is not lost, only not adopted
    automatically: `tick_state_library` carries it so the firm can review
    these notes and take the change deliberately.
    """
    pieces = _resolve_pieces(row.pieces or [], code_map, non_figure)

    if inherited is not None and inherited.trigger_keys:
        trigger_keys = list(inherited.trigger_keys)
    else:
        codes = []
        for piece in (row.pieces or []):
            for code in _codes_of(piece):
                if code not in codes:
                    codes.append(code)
        trigger_keys = resolve_keys(codes, code_map)

    tick_state = row.tick_state
    if tick_state == "tb_driven" and not trigger_keys:
        # Same reasoning as _resolve_pieces, at note level. A note wired to
        # nothing would never appear and nobody would be told why.
        tick_state = "manual"

    tick_from_library = tick_state
    preserved = False
    if inherited is not None and tick_state == "manual":
        if inherited.tick_state == "always":
            tick_state, preserved = "always", True
        elif inherited.tick_state == "tb_driven" and trigger_keys:
            tick_state, preserved = "tb_driven", True

    return {
        "key": row.key,
        "heading": row.heading,
        "tick_state": tick_state,
        "trigger_keys": trigger_keys,
        "pieces": pieces,
        "subsections": [],
        # Carried through for the builder and the gap report; the report
        # engine itself does not read these.
        "section_no": row.section_no,
        "section_name": row.section_name,
        "standards": row.standards,
        "trigger_text": row.trigger_text,
        "library_code": row.library_code,
        "tick_state_library": tick_from_library,
        "tick_preserved": preserved,
    }


def build_catalogue(version_id):
    """A library version as a flat list of numbered notes, each carrying its
    own sub-sections.

    NESTING. The workbook presents 91 rows: 45 numbered notes and 46
    sub-sections that belong underneath one of them. Which is which is the
    "Presented as" column, and the parent is "Sits inside".

    "Sits inside" is only honoured on a row marked Sub-section. It is filled
    in on 15 numbered notes as well, and there it is a fill-down artefact -
    it claims Prior period errors sits inside Material accounting policy
    information, and Lease liabilities inside Financial risk management.
    Reading it on those rows would bury a third of the notes inside two of
    them. A numbered note is top level; that is what being one means.

    An unresolvable parent is not dropped - it is promoted to a numbered note
    of its own, because a note the auditor can see and switch off is
    recoverable and a note silently missing from the accounts is not.
    """
    code_map, non_figure = load_line_code_map()

    rows = (NoteLibraryNote.query
            .filter_by(library_version_id=version_id)
            .order_by(NoteLibraryNote.sort_order, NoteLibraryNote.id).all())

    inherited = {e.key: e for e in NoteLibraryEntry.query.all()}

    parents, children = [], []
    for row in rows:
        if (row.presented_as or "").strip().lower() == "sub-section":
            children.append(row)
        else:
            parents.append(row)

    by_heading = {normalise_heading(r.heading): r.key for r in parents}
    notes = {r.key: _note_dict(r, code_map, non_figure, inherited.get(r.key))
             for r in parents}
    order = [r.key for r in parents]

    for row in children:
        parent_key = by_heading.get(normalise_heading(row.sits_inside))
        child = _note_dict(row, code_map, non_figure, inherited.get(row.key))
        if parent_key is None:
            log.warning("Library note %r says it sits inside %r, which is not "
                        "a numbered note in this version - promoting it",
                        row.key, row.sits_inside)
            notes[row.key] = child
            order.append(row.key)
            continue
        notes[parent_key]["subsections"].append(child)

    return [notes[key] for key in order]
