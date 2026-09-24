"""The customer's own notes, in their own order, under their own titles.

A customer who brings last year's signed accounts brings its notes too: which
ones there were, what each was called, and the order they came in. The report
follows that, not the library's catalogue:

  * every note the template had is switched on, retitled as the template
    titled it, and placed in the template's order - so the numbers come out as
    last year's (note 8 stays "Trade and other receivables");
  * a library note the template did not have is left out. If a balance-sheet
    line it covers now holds a balance (a payables balance where there were
    none) it is listed as NEW THIS YEAR for the preparer to decide; it stays
    off until they switch it on, and is never slipped in unmentioned;
  * a template note nothing in the library matches is flagged for the preparer.

What was decided is kept on the cover section so the builder can list it and
`check` can say whether the report still matches the template.
"""
import logging

log = logging.getLogger(__name__)


def _notes(report):
    from .reports import NOTE_PREFIX, is_statutory

    return [s for s in report.sections
            if s.section_key.startswith(NOTE_PREFIX) and not is_statutory(s)
            and s.parent_section_id is None and s.section_type != "statement"]


def _template_notes(financial_year):
    from ..models import PriorYearNote

    return (PriorYearNote.query.filter_by(financial_year_id=financial_year.id)
            .order_by(PriorYearNote.id).all())


def _carries_books(section, financial_year):
    """Whether a balance-sheet line this note covers holds a balance now.

    Profit and loss detail (other income, administrative expenses) is not a
    new disclosure: the template's own notes and face statements carry it, and
    it always has figures. Only a statement of financial position line with a
    balance - a payables balance where there were none - makes a note that
    the template never had one the accounts now need.
    """
    from . import conditions
    from .bindings import figures_for

    codes = {spec.get("note_code") for spec in
             (section.data_binding or {}).get("note_table_specs") or []
             if spec.get("note_code")}
    try:
        figures = figures_for(financial_year)
        for note_code in codes:
            for code in conditions.subject_codes(figures, note_code):
                if (figures.lines[code].get("Statement") == "SOFP"
                        and conditions.carries_balance(figures, code)):
                    return True
    except Exception:                                      # noqa: BLE001
        log.exception("Could not read the books behind %s", section.title)
    return False


def apply(report):
    """Reorder, retitle and switch the report's notes to the template's.
    Returns a short description, or None if there is no template to follow."""
    from .reports import NOTE_PREFIX

    financial_year = report.financial_year
    prior = _template_notes(financial_year)
    if not prior:
        return None
    notes = _notes(report)
    by_bare = {s.section_key[len(NOTE_PREFIX):]: s for s in notes}

    template, unmatched, used = [], [], set()
    for row in prior:
        section = by_bare.get(row.matched_key or "")
        if section is None or section.id in used:
            unmatched.append(f"{row.note_number or ''} {row.title}".strip())
            continue
        used.add(section.id)
        template.append((section, row))

    new, left_out = [], []
    for section in notes:
        if any(section is t[0] for t in template):
            continue
        if _carries_books(section, financial_year):
            new.append(section)
        else:
            left_out.append(section)

    for section, row in template:
        section.is_enabled = True
        section.title = (row.title or section.title).strip()
    for section in new + left_out:      # a new note waits for the preparer
        section.is_enabled = False

    slots = sorted(s.sort_order for s in notes)
    order = [t[0] for t in template] + new + left_out
    for slot, section in zip(slots, order):
        section.sort_order = slot

    cover = next((s for s in report.sections if s.section_key == "cover_page"), None)
    if cover is not None:
        binding = dict(cover.data_binding or {})
        binding["skeleton"] = {
            "template": [{"number": row.note_number, "title": (row.title or "").strip(),
                          "key": section.section_key} for section, row in template],
            "new": [s.title for s in new],
            "left_out": [s.title for s in left_out],
            "unmatched": unmatched,
        }
        cover.data_binding = binding
    return (f"followed its {len(template)} notes"
            + (f"; {len(new)} new note(s) added for review" if new else "")
            + (f"; {len(unmatched)} of its notes have no library match"
               if unmatched else ""))


def check(report):
    """Where the report's notes no longer match the template's, as messages.
    An empty list means the same titles in the same order."""
    from .reports import NOTE_PREFIX, ordered_sections, is_statutory

    cover = next((s for s in report.sections if s.section_key == "cover_page"), None)
    skeleton = ((cover.data_binding or {}).get("skeleton") if cover else None)
    if not skeleton:
        return []
    new_titles = set(skeleton.get("new") or [])
    printed = [s for s in ordered_sections(report, top_level_only=True)
               if s.is_enabled and s.section_key.startswith(NOTE_PREFIX)
               and not is_statutory(s) and s.section_type != "statement"
               and s.title not in new_titles]
    wanted = [n["title"] for n in skeleton["template"]]
    have = [s.title for s in printed]
    problems = []
    for missing in [t for t in wanted if t not in have]:
        problems.append(f"The template's note \"{missing}\" is not in the report.")
    for extra in [t for t in have if t not in wanted]:
        problems.append(f"\"{extra}\" is in the report but not in the template.")
    if not problems and [t for t in have if t in wanted] != wanted:
        problems.append("The notes are not in the template's order.")
    for name in skeleton.get("unmatched") or []:
        problems.append(f"The template's note \"{name}\" has no matching note "
                        f"in the library - add it by hand.")
    return problems


def review(report):
    """What the builder shows: the template's notes, checked against the
    report, and what was left for the preparer to decide."""
    cover = next((s for s in report.sections if s.section_key == "cover_page"), None)
    skeleton = ((cover.data_binding or {}).get("skeleton") if cover else None)
    if not skeleton:
        return None
    return {"mismatch": check(report),
            "new": skeleton.get("new") or [],
            "left_out": skeleton.get("left_out") or [],
            "count": len(skeleton.get("template") or [])}
