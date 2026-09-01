"""Audit report assembly and PDF export.

Sections come from config/report_sections.yaml, so swapping in the real
24-section template is a config change rather than a code change.

PDF export uses WeasyPrint when it's installed (Linux VPS). On Windows dev
machines WeasyPrint needs GTK libraries and is awkward to install, so the app
falls back to a print-optimised HTML page the user can save via Ctrl+P.
"""
import functools
import logging
import re
from datetime import date

import yaml
from flask import current_app

from ..extensions import db
from ..models import (AuditReport, AuditReportSection, FinancialStatement,
                      TrialBalanceAccount)
from . import notes as notes_service

NOTE_PREFIX = "note__"

log = logging.getLogger(__name__)


# Section captions on the face of a statement. The client's template groups
# the balance sheet under ASSETS / EQUITY AND LIABILITIES with a secondary
# caption beneath, so a "|" separates the major caption from the minor one.
# A statement type with no entry here prints no captions at all, which is how
# the Statement of Comprehensive Income is presented.
GROUP_HEADINGS = {
    "balance_sheet": {
        "non_current_assets": "ASSETS|Non-current assets",
        "current_assets": "Current assets",
        "equity": "EQUITY AND LIABILITIES|Capital and reserves",
        "non_current_liabilities": "Non-current liabilities",
        "current_liabilities": "Current liabilities",
    },
    "cash_flow": {
        "operating": "Cash flows from operating activities",
        "investing": "Cash flows from investing activities",
        "financing": "Cash flows from financing activities",
    },
    # The supplementary Detailed Profit and Loss Statement does caption its
    # blocks, unlike the statutory statement it expands.
    "profit_and_loss_detailed": {
        "cost_of_sales": "Less: Cost of sales",
        "operating_expenses": "Less: Operating expenses",
    },
}


@functools.lru_cache(maxsize=1)
def load_sections():
    """The structural sections: cover page, statements, detailed P&L.

    The 74-note FRS catalogue is separate - see load_notes_catalogue() -
    because a note's set is dynamic (which ones exist depends on the
    engagement) while these are fixed and always the same seven or eight.
    """
    path = current_app.config["CONFIG_DIR"] / "report_sections.yaml"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or []


def load_notes_catalogue():
    """The FRS disclosure library: every note that could apply to a
    single-entity Singapore Pte Ltd, in the order the firm's own template
    presents them.

    Read from `note_library_entries`, not the YAML file - the table is what
    an auditor's "save to the library" actually writes to, and a note
    added that way must be visible to the very next engagement without a
    server restart, which a cached read of a static file can never give.
    The 54 rows the spreadsheet supplied are seeded there once (see
    `flask seed-note-library`); this function does not distinguish them
    from ones an auditor added later - both are just library rows.

    Deliberately NOT cached - see above. Called once per report build,
    which is cheap enough that the extra correctness is worth it.
    """
    from ..models import NoteLibraryEntry

    rows = (NoteLibraryEntry.query
            .order_by(NoteLibraryEntry.sort_order).all())
    return [{
        "key": r.key,
        "heading": r.heading,
        "tick_state": r.tick_state,
        "trigger_keys": r.trigger_keys,
        "pieces": r.pieces or [],
        "subsections": r.subsections or [],
    } for r in rows]



@functools.lru_cache(maxsize=1)
def optional_line_keys():
    """Line keys that appear on the face only when they carry a balance.

    Read from config/statement_templates.yaml so the presentation rule lives
    beside the line it applies to.
    """
    path = current_app.config["CONFIG_DIR"] / "statement_templates.yaml"
    if not path.exists():
        return frozenset()
    with open(path, "r", encoding="utf-8") as handle:
        spec = yaml.safe_load(handle) or {}
    return frozenset(
        line["key"]
        for statement in spec.values() if isinstance(statement, dict)
        for line in (statement.get("lines") or []) if line.get("optional"))


def render_bindings(text: str, customer, financial_year,
                    chips: bool = False) -> str:
    """Substitute {{ binding }} placeholders in template section content.

    `chips=True` is for the in-place editor: each binding becomes an
    uneditable span that shows the real value but remembers which binding it
    came from. Without it, an auditor editing the rendered text would
    silently bake this year's company name into next year's template.

    A deliberately small, safe substitution — not Jinja — because this text is
    auditor-editable and must never be able to execute anything.
    """
    if not text:
        return ""

    values = {
        "customer.name": customer.name or "",
        "customer.legal_name": customer.legal_name or customer.name or "",
        "customer.uen": customer.uen or "—",
        "customer.address": ", ".join(filter(None, [
            customer.address_line1, customer.address_line2,
            f"Singapore {customer.postal_code}" if customer.postal_code else None,
        ])) or "—",
        "fy.year_label": financial_year.year_label or "",
        "fy.end_date": (financial_year.end_date.strftime("%d %B %Y")
                        if financial_year.end_date else ""),
        "fy.start_date": (financial_year.start_date.strftime("%d %B %Y")
                          if financial_year.start_date else ""),
        "customer.director": customer.directors or "",
        "customer.directors": customer.directors or "",
        "customer.secretary": customer.company_secretary or "",
        "customer.contact": customer.contact_person or "",
        "customer.phone": customer.phone or "",
        "customer.email": customer.email or "",
        "customer.currency": customer.books_currency or "SGD",
        "today": date.today().strftime("%d %B %Y"),
        "firm.name": "AltiusNXT Audit",
    }

    def replace(match):
        key = match.group(1).strip()

        if key not in values:
            # An unknown placeholder must never reach a client-facing report
            # looking like template code. Flag it so it is obvious in the
            # preview that something needs filling in.
            body, css = f"[{key} not set]", "missing-binding"
        elif not str(values[key]).strip():
            body, css = "[not provided]", "missing-binding"
        else:
            # Several directors are stored one per line; render them so.
            body, css = str(values[key]).replace(chr(10), "<br>"), ""

        if chips:
            classes = ("ph " + css).strip()
            return (f'<span class="{classes}" contenteditable="false" '
                    f'data-ph="{key}">{body}</span>')
        return f'<span class="{css}">{body}</span>' if css else body

    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", replace, text)


def ensure_report(financial_year) -> AuditReport:
    """Get or create the report for a financial year, seeding its sections."""
    report = AuditReport.query.filter_by(
        financial_year_id=financial_year.id).first()

    if report is not None:
        return report

    present = _present_keys(financial_year)

    report = AuditReport(
        financial_year_id=financial_year.id,
        title=f"Financial Statements — {financial_year.year_label}",
    )
    db.session.add(report)
    db.session.flush()

    order = 0
    for spec in load_sections():
        db.session.add(AuditReportSection(
            report_id=report.id,
            section_key=spec["key"],
            title=spec.get("title", spec["key"]),
            section_type=spec.get("type", "free_text"),
            sort_order=order,
            is_enabled=bool(spec.get("default_enabled", False)),
            content_html=spec.get("content", ""),
            data_binding={"statement_type": spec["statement_type"]}
            if spec.get("statement_type") else None,
        ))
        order += 1

    for note in load_notes_catalogue():
        db.session.add(_build_note_section(note, present, order, report.id))
        order += 1

    db.session.commit()
    return report


# --------------------------------------------------------------------------
# The FRS notes engine: selection, suppression, numbering
# --------------------------------------------------------------------------
#
# A note that explains a figure belongs in the accounts only when the figure
# is there. A company with no bank loan should not receive a borrowings note
# carrying wording about interest rates and covenants - that is a statement
# about the company, and it is not true.
#
# "There" means either year, not just this one: a balance held last year and
# nil this year must still show its note, with nil against last year's
# figure - a note cannot silently vanish just because the closing balance
# happens to be nil.

def _present_keys(financial_year):
    """Standard keys carrying a balance this year, or last year, or both."""
    current = (TrialBalanceAccount.query
               .filter_by(financial_year_id=financial_year.id)
               .filter(TrialBalanceAccount.standard_key.isnot(None))
               .all())
    present = {r.standard_key for r in current
               if (r.debit or 0) or (r.credit or 0)}

    for statement in FinancialStatement.query.filter_by(
            financial_year_id=financial_year.id).all():
        for line in statement.lines:
            if line.amount_previous:
                present.add(line.line_key)

    return present


def _piece_triggered(tick_state, trigger_keys, present):
    """Always fires; TB-driven fires if any of its keys is present; a Manual
    piece never fires on its own - the preparer switches it on by hand."""
    if tick_state == "always":
        return True
    if tick_state == "tb_driven":
        return any(key in present for key in (trigger_keys or []))
    return False


def _note_triggered(note, present):
    return _piece_triggered(note.get("tick_state"), note.get("trigger_keys"),
                            present)


TABLE_FORMS = {"Table", "Figure in note", "Narrative + table"}


def _assemble_note_content(note, present):
    """Build a note's starting text and figure tables from whichever of its
    pieces are triggered right now.

    This runs once, when the report is first created - the same point
    hand-authored content used to be copied in from report_sections.yaml.
    Like that content, what is produced here is then auditor-editable and
    frozen; it is not silently regenerated on every render, so an auditor's
    edit is never overwritten by a later trigger recalculation.
    """
    html_parts = []
    table_specs = []
    seen_table_keys = set()

    def add_piece(piece):
        if not _piece_triggered(piece.get("tick_state"), piece.get("tb_keys"),
                                present):
            return
        if piece.get("output_form") == "Narrative paragraph":
            wording = piece.get("wording")
            if wording:
                html_parts.append(f"<p>{wording}</p>")
        elif piece.get("output_form") in TABLE_FORMS:
            keys = piece.get("tb_keys") or []
            # Several pieces in one note (a movement schedule, a class
            # breakdown, a useful-lives table) can share the same trial
            # balance keys because the account-level breakdown is all this
            # engine can build so far - see the FRS build notes on PPE and
            # similar. Rendering that same flat breakdown under each
            # piece's own heading would print near-identical tables two or
            # three times, which reads as more wrong than showing it once.
            dedup_key = tuple(sorted(keys))
            if keys and dedup_key not in seen_table_keys:
                seen_table_keys.add(dedup_key)
                heading = piece.get("wording") or piece.get("requirement", "")
                table_specs.append({"source": "accounts", "keys": keys,
                                    "heading": heading, "total": ""})
            # No resolvable trial balance keys: nothing to compute, so
            # nothing is added. The auditor adds it by hand if it applies -
            # see readiness.py for the equivalent "flag, don't fabricate"
            # rule on the missing-documents side.

    for piece in note.get("pieces", []):
        add_piece(piece)

    for sub in note.get("subsections", []):
        if not _piece_triggered(sub.get("tick_state"), sub.get("trigger_keys"),
                                present):
            continue
        sub_parts = []
        for piece in sub.get("pieces", []):
            if (_piece_triggered(piece.get("tick_state"), piece.get("tb_keys"),
                                 present)
                    and piece.get("output_form") == "Narrative paragraph"
                    and piece.get("wording")):
                sub_parts.append(f"<p>{piece['wording']}</p>")
        if sub_parts:
            html_parts.append(f"<h4>{sub['heading']}</h4>")
            html_parts.extend(sub_parts)

    if not html_parts:
        html_parts.append("<p><em>Write this note here.</em></p>")

    return "\n".join(html_parts), table_specs


def _build_note_section(note, present, sort_order, report_id):
    content_html, table_specs = _assemble_note_content(note, present)
    return AuditReportSection(
        report_id=report_id,
        section_key=f"{NOTE_PREFIX}{note['key']}",
        title=note["heading"],
        section_type="free_text",
        sort_order=sort_order,
        is_enabled=_note_triggered(note, present),
        content_html=content_html,
        data_binding={"note_table_specs": table_specs} if table_specs else None,
    )


def ordered_sections(report, *, top_level_only=False):
    """`report.sections` regrouped so a sub-note always sits directly after
    its parent, whatever its own sort_order says relative to unrelated
    sections.

    A child's sort_order only orders it among its OWN siblings (several
    sub-notes attached to the same parent); it says nothing about where
    among the top-level notes the whole group falls. Both numbering and
    rendering need that same grouping, so it lives here once rather than
    twice.
    """
    top = [s for s in sorted(report.sections, key=lambda s: s.sort_order)
          if s.parent_section_id is None]
    if top_level_only:
        return top

    out = []
    for section in top:
        out.append(section)
        out.extend(sorted(section.children, key=lambda c: c.sort_order))
    return out


def note_number_map(report):
    """{note key: printed number}, computed fresh from whichever notes are
    enabled right now, in seed order. "11.1" for a sub-note attached to
    note 11, otherwise a plain "11".

    Never stored. A note that gets unticked must leave no gap behind it, and
    a statement line's "see Note N" must always match the number actually
    printed - both are only true if this is recalculated on every render
    rather than fixed at creation.
    """
    def is_note(s):
        return (s.section_type != "statement"
                and (s.section_key.startswith(NOTE_PREFIX)
                    or s.section_key.startswith("custom_")))

    def bare(s):
        return (s.section_key[len(NOTE_PREFIX):]
               if s.section_key.startswith(NOTE_PREFIX) else s.section_key)

    mapping = {}
    n = 0
    for section in ordered_sections(report, top_level_only=True):
        if not (is_note(section) and section.is_enabled):
            continue
        n += 1
        num = str(n)
        mapping[bare(section)] = num
        mapping[section.section_key] = num

        children = [c for c in sorted(section.children, key=lambda c: c.sort_order)
                   if is_note(c) and c.is_enabled]
        for i, child in enumerate(children, start=1):
            child_num = f"{num}.{i}"
            mapping[bare(child)] = child_num
            mapping[child.section_key] = child_num
    return mapping


def content_gaps(report, financial_year):
    """Where the FRS library, drawn strictly from
    AuditMate_FullFRS_Disclosure_Requirements_1.xlsx, does not cover what
    this engagement's own statements need.

    Two different failures, kept apart because they need different fixes:

    MISSING - a line is printing on the face of a statement, but no note in
    the library explains it at all. The library was never given content for
    this - it is not a case of the trigger failing to fire, there is simply
    nothing there to trigger.

    THIN - a note that is always required, in every engagement, has next to
    no content behind it - a single edge-case figure standing in for what
    should be a real policy paragraph.

    Deliberately mechanical, not a judgement call: the library is never
    padded with invented wording to make a gap disappear. The auditor sees
    exactly what is short and adds it themselves, the same way a note not on
    the list gets added today.
    """
    from .statements import load_templates

    catalogue = {n["key"]: n for n in load_notes_catalogue()}
    templates = load_templates()

    missing = []
    for statement_type in ("profit_and_loss", "balance_sheet"):
        statement = FinancialStatement.query.filter_by(
            financial_year_id=financial_year.id,
            statement_type=statement_type).first()
        lines_by_key = ({l.line_key: l for l in statement.lines}
                        if statement else {})

        for spec in templates.get(statement_type, {}).get("lines", []):
            if spec.get("subtotal") or spec.get("total") or spec.get("detail"):
                continue
            if spec.get("no_note"):
                continue
            note_key = spec.get("note")
            if note_key and note_key in catalogue:
                continue      # has a real note behind it

            line = lines_by_key.get(spec["key"])
            printing = bool(line and (line.amount_current or line.amount_previous))
            if not spec.get("optional") or printing:
                missing.append({"line": spec.get("label", spec["key"]),
                               "key": spec["key"], "statement": statement_type})

    # Group by underlying gap: "Other Payables" and "Accruals" both point at
    # the one absent note, not two separate ones.
    grouped_missing = []
    if missing:
        labels = sorted({m["line"] for m in missing})
        grouped_missing.append({
            "lines": labels,
            "keys": sorted({m["key"] for m in missing}),
            "detail": ("No note in the library explains " + ", ".join(labels)
                      + " - there is nothing in the spreadsheet covering it, "
                        "not a trigger that failed to fire."),
        })

    thin = []
    enabled_note_keys = {
        s.section_key[len(NOTE_PREFIX):]
        for s in report.sections
        if s.is_enabled and s.section_key.startswith(NOTE_PREFIX)
    }
    for key in sorted(enabled_note_keys):
        note = catalogue.get(key)
        if not note or note.get("tick_state") != "always":
            continue
        pieces = list(note.get("pieces") or [])
        for sub in note.get("subsections", []):
            pieces += sub.get("pieces") or []
        live = [p for p in pieces if p.get("tick_state") in ("always", "tb_driven")]
        narrative = [p for p in live if p.get("output_form") == "Narrative paragraph"]
        if len(live) <= 1 and not narrative:
            forms = ", ".join(p["output_form"].lower() for p in live) or "nothing"
            thin.append({
                "note": note["heading"],
                "detail": (f"This note is required for every engagement, "
                          f"but the library gives it only {forms} - no "
                          f"policy or description of what the figure "
                          f"actually is."),
            })

    return {"missing": grouped_missing, "thin": thin,
            "has_gaps": bool(grouped_missing or thin)}


def mapped_accounts(financial_year):
    """This engagement's trial balance, one row per standard line that
    actually carries a balance - the checklist an auditor picks from when
    a new note needs real figures instead of hand-typed ones.

    Deduplicated by standard_key: several trial balance accounts can map to
    the same line (two bank accounts both feed cash_and_equivalents), and
    the note table is built from the line, not the individual accounts.
    """
    from .statements import load_templates
    from decimal import Decimal

    labels = {}
    for statement in load_templates().values():
        if not isinstance(statement, dict):
            continue
        for line in statement.get("lines") or []:
            labels[line["key"]] = line.get("label", line["key"])

    rows = (TrialBalanceAccount.query
            .filter_by(financial_year_id=financial_year.id)
            .filter(TrialBalanceAccount.standard_key.isnot(None))
            .all())

    totals = {}
    for r in rows:
        net = Decimal(str((r.debit or 0))) - Decimal(str((r.credit or 0)))
        totals[r.standard_key] = totals.get(r.standard_key, Decimal("0")) + net

    return sorted(
        ({"key": key, "label": labels.get(key, key), "amount": amount}
         for key, amount in totals.items() if amount),
        key=lambda a: a["label"])


def attachable_notes(report):
    """Top-level enabled notes an auditor can attach a new sub-note under -
    the parent-note dropdown in the "add a note" form. Numbered exactly as
    they will print, using the same live count as everywhere else.
    """
    numbers = note_number_map(report)
    out = []
    for section in ordered_sections(report, top_level_only=True):
        if not section.is_enabled:
            continue
        if section.section_type == "statement":
            continue
        if not (section.section_key.startswith(NOTE_PREFIX)
                or section.section_key.startswith("custom_")):
            continue
        num = numbers.get(section.section_key)
        if num:
            out.append({"id": section.id, "number": num, "title": section.title})
    return out


@functools.lru_cache(maxsize=1)
def _spec_index():
    """config/report_sections.yaml keyed by section key.

    Looked up at render time rather than copied into the database when the
    report is seeded, so editing the YAML changes existing reports too.
    """
    return {spec["key"]: spec for spec in load_sections()}


def section_payload(section, customer, financial_year, chips: bool = False):
    """Build what a section needs to render: text, tables and/or a statement."""
    spec = _spec_index().get(section.section_key, {})

    payload = {
        "section": section,
        "statement": None,
        "html": "",
        "tables": [],
        # The Detailed Profit and Loss Statement shows the breakdown lines
        # that the face of the statutory statement summarises away.
        "detailed": bool(spec.get("detailed")),
        "footnote": spec.get("footnote"),
    }

    if section.section_type == "statement":
        statement_type = (section.data_binding or {}).get("statement_type")
        if statement_type:
            payload["statement"] = FinancialStatement.query.filter_by(
                financial_year_id=financial_year.id,
                statement_type=statement_type).first()
    else:
        payload["html"] = render_bindings(section.content_html or "",
                                          customer, financial_year,
                                          chips=chips)
        note_table_spec = spec.get("note_table")
        if note_table_spec is None and section.data_binding:
            note_table_spec = section.data_binding.get("note_table_specs")
        payload["tables"] = notes_service.build_tables(
            note_table_spec, financial_year)
        apply_note_overrides(section, payload["tables"])

    return payload


def apply_note_overrides(section, tables):
    """Lay the auditor's edits over the computed note tables.

    Note tables are recomputed from the trial balance on every render, so an
    edit cannot live on the row. It lives in `report_figure_overrides`,
    addressed by position, and is put back here.

    An override is applied only when the row at that position still has the
    label it had when the edit was made. If the note has since been rebuilt
    with different accounts, the edit is shown as stale rather than dropped
    onto whichever figure happens to sit there now - silently moving an
    auditor's correction onto a different account is the one outcome worth
    engineering against.
    """
    from ..models import ReportFigureOverride

    if not tables:
        return

    overrides = ReportFigureOverride.query.filter_by(
        report_id=section.report_id, section_key=section.section_key).all()
    if not overrides:
        return

    by_position = {(o.table_index, o.row_index): o for o in overrides}

    for table_index, table in enumerate(tables):
        for row_index, row in enumerate(table.get("rows", [])):
            override = by_position.get((table_index, row_index))
            if override is None:
                continue

            row["override_id"] = override.id
            if not override.matches(row):
                # The row moved. Say so on the face of the report rather
                # than applying the figure to the wrong account.
                row["stale_override"] = override.anchor_label
                continue

            if override.label_override is not None:
                row["original_label"] = row.get("label")
                row["label"] = override.label_override
                row["label_overridden"] = True
            if override.amount_override is not None:
                row["computed_current"] = row.get("current")
                row["current"] = override.amount_override
                row["overridden"] = True


def weasyprint_available() -> bool:
    try:
        import weasyprint      # noqa: F401
        return True
    except Exception:          # noqa: BLE001  (import can fail on missing GTK)
        return False


def render_pdf(html: str, base_url: str = None) -> bytes:
    """Render assembled HTML into PDF bytes. Raises if WeasyPrint is absent."""
    import weasyprint
    return weasyprint.HTML(string=html, base_url=base_url).write_pdf()
