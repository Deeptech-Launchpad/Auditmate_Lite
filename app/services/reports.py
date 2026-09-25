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
                      PriorYearNote, TrialBalanceAccount)
from . import notes as notes_service
from . import period as period_module

NOTE_PREFIX = "note__"

log = logging.getLogger(__name__)


# Stands in for a note nobody has written yet - a library note whose every
# narrative piece stayed untriggered, or a note added by hand a moment ago.
# It exists so the note does not render as a heading with nothing under it.
#
# Left standing it is NOT content: it is the note still waiting to be
# written, and these five words must never reach a client's financial
# statements. content_gaps() reports any note still holding it.
UNWRITTEN_NOTE_HTML = "<p><em>Write this note here.</em></p>"

# Both forms have been written into reports - the library builder used the
# italic one, the add-a-note route a plain one - and reports already created
# carry whichever was current at the time. Recognising both is what stops a
# note that has never been written from counting as written.
UNWRITTEN_NOTE_FORMS = {
    UNWRITTEN_NOTE_HTML,
    "<p>Write this note here.</p>",
}


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


def load_notes_catalogue(financial_year=None):
    """The FRS disclosure library: every note that could apply to a
    single-entity Singapore Pte Ltd, in the order the firm's own template
    presents them.

    WHICH library depends on the engagement. A financial year pinned to a
    notes library version reports under that version for the rest of its
    life - see `note_library.pin_version`. That is the whole point of
    versioning: FRS 118 replaces FRS 1 for periods beginning 1 January 2027,
    and an FY2026 and an FY2027 engagement open in the same week must each
    get the library that was in force for its own period.

    Passing no financial year, or one that was never pinned, falls back to
    the flat `note_library_entries` table - which is every engagement that
    existed before versioning, and is why introducing it changed nothing for
    work already in progress.

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

    def flat(rows):
        return [{
            "key": r.key,
            "heading": r.heading,
            "tick_state": r.tick_state,
            "trigger_keys": r.trigger_keys,
            "pieces": r.pieces or [],
            "subsections": r.subsections or [],
        } for r in rows]

    version_id = getattr(financial_year, "library_version_id", None)
    if version_id is None:
        return flat(NoteLibraryEntry.query
                    .order_by(NoteLibraryEntry.sort_order).all())

    from . import note_library

    catalogue = note_library.build_catalogue(version_id)

    # A note an auditor wrote belongs to the firm, not to a library version,
    # so it survives every import and is offered to an engagement on any
    # version. Appended rather than merged: if the new library happens to
    # carry a note with the same key, the library's is the one the engagement
    # reports under and the auditor's own is not silently duplicated beside it.
    known = {n["key"] for n in catalogue}
    catalogue.extend(flat(
        NoteLibraryEntry.query
        .filter_by(source="auditor_added")
        .filter(NoteLibraryEntry.key.notin_(known) if known else True)
        .order_by(NoteLibraryEntry.sort_order).all()))
    return catalogue


def visible_statement_lines(lines, detailed=False):
    """The lines of a statement that actually reach the face.

    Two things keep a line off it:

    A BREAKDOWN of a subtotal shown above it. Only the Detailed Profit and
    Loss Statement expands those; the statutory face shows the subtotal.

    A line the company has NO BALANCE ON IN EITHER YEAR. It is a line the
    template offers and this company does not use, and a template's worth of
    them buries the figures that are there - the profit and loss carries 36
    lines and an ordinary small company uses barely half. A figure in even
    one of the two years always prints, so a balance that arrived or went
    away stays visible and the face still adds up to its own subtotals.

    A subtotal is not an account, so nil alone does not drop it - it goes
    only when nothing in its group printed either, which is the difference
    between "this company has no investing activities at all" (the heading,
    the lines under it and the subtotal all go together) and "its investing
    activities happen to net to nil this year" (they are shown, and the
    subtotal shows nil beneath them). The grand total always prints: it is
    the statement's bottom line even when it is nil.

    Group headings need no handling of their own. The template prints one
    when it reaches the first visible line of a group, so a group with
    nothing left in it never announces itself.
    """
    def carries(line):
        return bool(line.effective_amount or line.amount_previous)

    candidates = [line for line in lines if detailed or not line.is_detail]

    live_groups = {line.group_key for line in candidates
                   if carries(line) and not (line.is_total or line.is_subtotal)}

    visible = []
    for line in candidates:
        if carries(line) or line.is_total:
            visible.append(line)
        elif line.is_subtotal and line.group_key in live_groups:
            visible.append(line)
    return visible


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
        # "for the year ended DATE" against "for the period from X to Y" -
        # never assumes twelve months. See services.period.heading_wording.
        "fy.period_heading": period_module.heading_wording(
            financial_year.start_date, financial_year.end_date),
        "customer.director": customer.directors or "",
        "customer.directors": customer.directors or "",
        "customer.secretary": customer.company_secretary or "",
        "customer.contact": customer.contact_person or "",
        "customer.phone": customer.phone or "",
        "customer.email": customer.email or "",
        "customer.currency": customer.books_currency or "SGD",
        "customer.principal_activities": customer.principal_activities or "",
        "today": date.today().strftime("%d %B %Y"),
        "firm.name": "AltiusNXT Audit",
    }

    # The FRS library's own blanks - credit terms, the days-overdue
    # thresholds. Resolved per client (its own answer, else the firm's), and
    # deliberately NOT defaulted to anything here: a key with no answer is
    # left out, so `replace` below prints "[not set]" exactly as it does for
    # any other unfilled binding rather than inventing a policy.
    from . import disclosure_settings

    for key, value in disclosure_settings.resolved(customer).items():
        values[f"firm.{key}"] = value

    # The related party register, for a preparer writing the related party
    # note. Offered as a substitution, not imposed: FRS 24 wants the nature
    # of the relationship described, and the library gives no binding for a
    # party's name, so the sentence stays the preparer's to write. Only
    # parties with something confirmed against them in the books appear -
    # the rest are on the register but this engagement's figures cannot
    # point at them. Left out entirely when there are none, so the binding
    # prints as unfilled rather than as a confident blank.
    # The Fields sheet's twelve preparer blanks - the nature of a
    # contingent liability, what an asset held for sale is. Each names the
    # exact paragraph it sits in, and until the preparer inputs page
    # existed nothing offered them: the paragraph printed "[contingent
    # liability nature not provided]" with no way to provide it. An
    # unanswered one is still left out, so it keeps saying so.
    from . import preparer_inputs as input_service

    values.update(input_service.values_for_bindings(financial_year))

    # The library's statutory documents name fields the client and firm
    # records answer. None had a source, so the directors' statement printed
    # "[members not provided]" and "[signing directors not provided]" although
    # the customer record held the director.
    directors = [d.strip() for d in (customer.directors or "").splitlines()
                 if d.strip()]
    names = (", ".join(directors[:-1]) + " and " + directors[-1]
             if len(directors) > 1 else "".join(directors))
    values["field.director_names"] = names
    values["field.signing_directors"] = names
    values["field.members"] = _members_word(financial_year)
    values["field.report_date"] = date.today().strftime("%d %B %Y")
    values["field.place_of_signature"] = (
        current_app.config.get("FIRM_PLACE_OF_SIGNATURE") or "Singapore")
    # The firm's own settings first (typed once in Settings, or read from a
    # compilation report), then the deployment's .env.
    values["field.practitioner_name"] = (
        values.get("firm.practitioner_name")
        or current_app.config.get("FIRM_PRACTITIONER_NAME") or "")
    values["field.practitioner_address"] = (
        values.get("firm.practitioner_address")
        or current_app.config.get("FIRM_PRACTITIONER_ADDRESS") or "")

    from . import related_parties as related_service

    confirmed = related_service.parties_in_play(financial_year)
    if confirmed:
        values["related.names"] = ", ".join(party.name for party in confirmed)
        values["related.parties"] = ", ".join(
            f"{party.name} ({party.kind_label.lower()})" for party in confirmed)

    def replace(match):
        key = match.group(1).strip()

        # A library blank the firm has not answered yet. Named so the
        # preview says which setting is missing, not just that something is.
        if key.startswith("firm.") and key not in values:
            body, css = f"[{key[5:].replace('_', ' ')} not set]", "missing-binding"
            if chips:
                return (f'<span class="ph missing-binding" '
                        f'contenteditable="false" data-ph="{key}">{body}</span>')
            return f'<span class="{css}">{body}</span>'

        # A blank in library 2.x wording. An amount the Fields sheet ties to
        # a mapped line is read from the books now, so it follows the trial
        # balance; anything else - the holding company's name, an item the
        # preparer describes - is named in words, never as template code.
        if key.startswith("field.") and key not in values:
            amount = _field_amount(key[6:], financial_year, customer)
            words = key[6:].replace("_", " ").lower()
            if amount is not None:
                body, css = amount, ""
            else:
                body, css = f"[{words} not provided]", "missing-binding"
            if chips:
                classes = ("ph " + css).strip()
                return (f'<span class="{classes}" contenteditable="false" '
                        f'data-ph="{key}">{body}</span>')
            return f'<span class="{css}">{body}</span>' if css else body

        if key not in values:
            # An unknown placeholder must never reach a client-facing report
            # looking like template code. Flag it so it is obvious in the
            # preview that something needs filling in.
            body, css = f"[{key} not set]", "missing-binding"
        elif not str(values[key]).strip():
            # Named, so the preparer knows which field on the client record
            # to fill in rather than hunting for it.
            body = f"[{key.split('.')[-1].replace('_', ' ')} not provided]"
            css = "missing-binding"
        else:
            # Several directors are stored one per line; render them so.
            body, css = str(values[key]).replace(chr(10), "<br>"), ""
            body = _without_repeated_unit(body, match.string[match.end():])

        if chips:
            classes = ("ph " + css).strip()
            return (f'<span class="{classes}" contenteditable="false" '
                    f'data-ph="{key}">{body}</span>')
        return f'<span class="{css}">{body}</span>' if css else body

    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", replace, text)


def _members_word(financial_year):
    """"member" for a company with one shareholder, otherwise "members".

    Read from the share capital accounts on the trial balance - one account,
    one holder, as in the signed set this replaces ("to the member of the
    Company") - until a share register says otherwise.
    """
    holders = (TrialBalanceAccount.query
               .filter_by(financial_year_id=financial_year.id)
               .filter(TrialBalanceAccount.standard_key == "share_capital")
               .count())
    return "member" if holders == 1 else "members"


# A firm setting whose value carries its own unit, dropped into a sentence
# that supplies the unit as well.
_TRAILING_UNIT = re.compile(r"\s+([A-Za-z]+)\s*$")


def _without_repeated_unit(body, rest):
    """Drop a unit from a substituted value when the sentence repeats it.

    The firm settings are stored the way a person would say them - "30
    days", "30 to 60 days" - because most of the library's sentences want
    them whole: "credit terms of 30 to 60 days". Four sentences do not.
    They write "more than {sicr_days} days past due", supplying the unit
    themselves, and the two conventions met in the accounts as "more than
    30 days days past due".

    Rather than keeping two copies of every setting, one with the unit and
    one without, the word is dropped where the sentence is about to say it
    again. It only ever removes a word the reader is about to read anyway.
    """
    unit = _TRAILING_UNIT.search(body or "")
    if not unit:
        return body
    following = (rest or "").lstrip()
    word = unit.group(1)
    if following[:len(word)].lower() == word.lower() and (
            len(following) == len(word)
            or not following[len(word)].isalnum()):
        return body[:unit.start()]
    return body


def _field_amount(name, financial_year, customer):
    """A library blank supplied by a mapped line, formatted - or None.

    None when the Fields sheet does not tie the blank to a line, or the line's
    figure cannot be stated (a held figure is never printed as a number).
    `_PY` fields are last year's figure. Shown unsigned: the wording around
    it already says whether it is a loss or a liability.
    """
    from decimal import Decimal

    from . import bindings

    figures = bindings.figures_for(financial_year)
    version = bindings._version_for(financial_year)
    rows = version.sheet("Fields") if version else []
    row = next((r for r in rows or []
                if str(r.get("Field") or "").lower() == name.lower()), None)
    if not row or str(row.get("Supplied by") or "").lower() != "mapped line":
        return None
    code = row.get("Line code")
    if not code or code == "-":
        return None
    offset = 1 if name.lower().endswith("_py") else 0
    value = figures.resolve(code, offset)
    if not isinstance(value, Decimal):
        return None
    if not value:
        return "nil"
    symbol = "S$" if (customer.books_currency or "SGD") == "SGD" else (customer.books_currency + " ")
    return f"{symbol}{abs(value):,.0f}"


def _template_section(spec, report_id, order) -> AuditReportSection:
    """One report section built from a library section spec."""
    return AuditReportSection(
        report_id=report_id,
        section_key=spec["key"],
        title=spec.get("title", spec["key"]),
        section_type=spec.get("type", "free_text"),
        sort_order=order,
        is_enabled=bool(spec.get("default_enabled", False)),
        content_html=spec.get("content", ""),
        data_binding={"statement_type": spec["statement_type"]}
        if spec.get("statement_type") else None,
    )


def add_missing_template_sections(report) -> list:
    """Add library sections this report was created before.

    Sections are seeded once, when a report is first created, so a section
    added to the library afterwards never reached the reports that already
    existed. A statement page could sit in the template and be absent from
    the finished document, with nothing on screen to say why.

    Matched on the section key, so a section already there keeps its wording,
    its position and whether it is switched on. A library section can only be
    switched off, never deleted (see reports.delete_section), so a key missing
    from a report was never seeded rather than deliberately removed - which is
    what makes adding it back safe rather than a resurrection.
    """
    specs = load_sections()
    existing = {s.section_key: s for s in report.sections}
    missing = [(index, spec) for index, spec in enumerate(specs)
               if spec["key"] not in existing]
    if not missing:
        return []

    sections = list(report.sections)
    added = []

    for index, spec in missing:
        # Slotted in after the nearest library section before it that this
        # report does have, so it lands where the library puts it instead of
        # after the notes at the very end. Anything an auditor dragged into a
        # different order keeps its relative position either way.
        after = None
        for earlier in reversed(specs[:index]):
            if earlier["key"] in existing:
                after = existing[earlier["key"]]
                break
        position = (after.sort_order + 1) if after is not None else 0

        for section in sections:
            if section.sort_order >= position:
                section.sort_order += 1

        created = _template_section(spec, report.id, position)
        db.session.add(created)
        sections.append(created)
        existing[spec["key"]] = created
        added.append(created)

    db.session.commit()
    log.info("Report %s gained %s section(s) added to the library later: %s",
             report.id, len(added), ", ".join(s.section_key for s in added))
    return added


def ensure_report(financial_year) -> AuditReport:
    """Get or create the report for a financial year, seeding its sections."""
    report = AuditReport.query.filter_by(
        financial_year_id=financial_year.id).first()

    # The company's details from the template it brought, where the record is
    # still empty (no AI involved). A template uploaded before this existed, or
    # while the AI service was down, otherwise left every "[not provided]".
    try:
        from . import template_details
        if template_details.fill_missing(financial_year.customer):
            db.session.commit()
    except Exception:                                      # noqa: BLE001
        log.exception("Could not read the company's details from its template")
    try:
        from . import line_codes
        line_codes.refresh_defaults(financial_year)
    except Exception:                                      # noqa: BLE001
        log.exception("Could not refresh the accounts' line codes")

    if report is not None:
        add_missing_template_sections(report)
        return report

    # An engagement reads from the notes library version in force for its
    # year end, attached the first time its report is made. Nothing else in
    # the app did that - only a command-line backfill - so every customer
    # created in the browser fell back to the old flat library, which has no
    # figure bindings: notes printed unfilled, repeated one table under
    # several notes, and left figures "Incomplete" that the documents held.
    from . import note_library
    note_library.pin_version(financial_year)
    db.session.flush()

    # Last year's figures at the grain the notes need, from the notes of the
    # signed accounts. Before the notes are built, so they see them.
    from . import signed_notes
    try:
        signed_notes.fill(financial_year)
    except Exception:                                      # noqa: BLE001
        log.exception("Could not read last year's note figures")
    _rematch_prior_notes(financial_year)

    present = _present_keys(financial_year)

    report = AuditReport(
        financial_year_id=financial_year.id,
        title=f"Financial Statements — {financial_year.year_label}",
    )
    db.session.add(report)
    db.session.flush()

    order = 0
    for spec in load_sections():
        db.session.add(_template_section(spec, report.id, order))
        order += 1

    period = (financial_year.start_date, financial_year.end_date)
    previous_period = _previous_period_of(financial_year)
    for note in load_notes_catalogue(financial_year):
        db.session.add(_build_note_section(
            note, present, order, report.id,
            first_year=bool(financial_year.is_first_year), period=period,
            previous_period=previous_period, financial_year=financial_year))
        order += 1

    db.session.flush()
    _arrange_statutory(report)
    _match_customer_template(report, financial_year)

    db.session.commit()
    return report


def _rematch_prior_notes(financial_year):
    """Match last year's stored note headings to the pinned library afresh.

    Matched once, when the signed accounts were read - against whatever
    library and matcher there were then. A heading stored unmatched stayed
    "NO NOTE" for ever, even after the matcher learned "Finance cost" is
    "Finance costs". No AI is involved, so it is cheap to redo on every build.
    """
    from .extraction import _normalise_heading, match_heading

    try:
        library = {_normalise_heading(n["heading"]): n["key"]
                   for n in load_notes_catalogue(financial_year)}
        if not library:
            return
        for prior in PriorYearNote.query.filter_by(
                financial_year_id=financial_year.id).all():
            key = match_heading(prior.title, library)
            if key != prior.matched_key:
                prior.matched_key = key
        db.session.flush()
    except Exception:                                      # noqa: BLE001
        log.exception("Could not re-match last year's notes")


def regenerate_report(financial_year):
    """Throw the report away and build it again from what is on file now.

    A report's sections and note tables are fixed when it is created, so a
    change to the trial balance, the notes library or the customer's template
    reached only reports made afterwards. The only way to refresh one was to
    delete it in the database.

    Everything typed into the report - note wording, figure overrides,
    section switches, the cover labels - goes with it, so the caller must have
    said so. The statements are not touched: they come from the approved
    trial balance, and are rebuilt there.
    """
    from ..models import ReportFigureOverride

    report = AuditReport.query.filter_by(
        financial_year_id=financial_year.id).first()
    if report is not None:
        ReportFigureOverride.query.filter_by(report_id=report.id).delete()
        db.session.delete(report)
        db.session.flush()
    return ensure_report(financial_year)


# The library's statutory documents. "Presented as: Statutory document" in its
# Notes sheet: the directors' statement, the compilation report, and the
# signature block that "sits inside" the directors' statement. Not notes -
# numbered as notes they pushed every real note down by three.
STATUTORY_PREFIX = NOTE_PREFIX + "S0"


def is_statutory(section):
    return section.section_key.startswith(STATUTORY_PREFIX)


def _arrange_statutory(report):
    """Put the library's statutory documents where a set of accounts has them.

    The compilation report first, then the directors' statement with its
    signature block inside it, then everything else as before. The library's
    directors' statement replaces the app's built-in one: printing both put
    the same statement in the accounts twice.
    """
    by_key = {s.section_key: s for s in report.sections}
    s01 = by_key.get(NOTE_PREFIX + "S01_DIRECTORS_STATEMENT")
    s02 = by_key.get(NOTE_PREFIX + "S02_COMPILATION_REPORT")
    s03 = by_key.get(NOTE_PREFIX + "S03_STATEMENT_BY_DIRECTORS_SIG")

    fixed = by_key.get("directors_statement")
    if s01 is not None and s01.is_enabled and fixed is not None:
        fixed.is_enabled = False

    cover = by_key.get("cover_page")
    # The signature block "sits inside" the directors' statement: kept as its
    # own section straight after it, so the statement's tables (the directors'
    # shareholdings) print between its text and the signatures.
    front = [x for x in (cover, s02, s01, s03 if s01 is not None else None)
             if x is not None]
    rest = [x for x in sorted(report.sections, key=lambda x: (x.sort_order, x.id))
            if x not in front]
    for position, section in enumerate(front + rest):
        section.sort_order = position


def _match_customer_template(report, financial_year):
    """Shape a NEW report like the customer's own template, and say so.

    Only at creation: after that the preparer owns the section switches, and
    a rebuild must not undo a choice they made on purpose.
    """
    from . import template_follow, template_outline

    # The customer's own template if one was uploaded; otherwise last year's
    # signed accounts on file, which is the same thing - the report they
    # filed. A customer who chose the standard format explicitly keeps it.
    chosen = financial_year.customer.report_template_path
    if chosen == "STANDARD":
        return
    path = template_follow._template_path(financial_year)
    if path is None:
        from pathlib import Path
        for document in financial_year.documents:
            if (document.category == "signed_accounts"
                    and Path(str(document.storage_path)).suffix.lower() == ".pdf"
                    and Path(str(document.storage_path)).exists()):
                path = Path(str(document.storage_path))
                break
    template = str(path) if path else chosen

    try:
        changed = template_outline.apply_to_report(report, template)
    except Exception:                                      # noqa: BLE001
        log.exception("Could not match the report to the customer's template")
        return
    if changed:
        log.info("Report %s follows the customer's template: %s",
                 report.id, changed)
        from flask import flash, has_request_context
        if has_request_context():
            flash(f"Sections follow this customer's template: {changed}. "
                  f"Switch any back on in the Sections list.", "info")


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

# A blank in library wording that only a person can fill: {ITEM}, ${AMOUNT}.
_OPEN_BLANK = re.compile(r"\$?\{[A-Z][A-Z_]*\}")

TABLE_PLACEHOLDER = re.compile(r"^\s*\[table ([^\]]+)\]\s*$")


def _all_pieces(note):
    pieces = list(note.get("pieces") or [])
    for sub in note.get("subsections") or []:
        pieces.extend(sub.get("pieces") or [])
    return pieces


def _first_period_wording(period):
    """The comparative-information paragraph for a first financial period.

    States the period, because whether it ran to twelve months is exactly
    what a reader cannot assume of a first set of accounts and exactly what
    the firm asked to see said. Where the dates are not known yet the
    sentence is left for the preparer rather than guessed at.
    """
    start, end = period if period else (None, None)
    if start and end:
        months = period_module.approx_months(start, end)
        length = (f"a period of approximately {months} months"
                  if months != 12 else "a twelve-month period")
        covered = (f"from {start.strftime('%d %B %Y')} to "
                   f"{end.strftime('%d %B %Y')}, being {length}")
    else:
        covered = ("from the date of incorporation to the financial year "
                   "end [state the period]")

    return (f"These financial statements cover the period {covered}, and are "
            f"the Company's first financial statements since incorporation. "
            f"Accordingly, no comparative figures are presented, and the "
            f"amounts reported are not necessarily comparable with those of "
            f"a subsequent full financial year.")


def _previous_period_of(financial_year):
    """The linked previous year's (start, end), or (None, None).

    Only ever the dates of an actual previous FinancialYear this system
    knows about - see FinancialYear.previous_year_id, set when a year is
    created or edited. A document supplying comparative figures without a
    linked engagement carries no confirmed period length, so there is
    nothing here to compare against and nothing is invented.
    """
    if financial_year is None:
        return (None, None)
    previous = getattr(financial_year, "previous_year", None)
    if previous is None:
        return (None, None)
    return (previous.start_date, previous.end_date)


def _comparative_length_note(period, previous_period):
    """None, or a sentence stating that this period and the comparative
    period are not the same length.

    Silent whenever the previous year isn't linked by date, or the two
    periods round to the same number of months - nothing to flag when
    there is nothing to compare, or when there is nothing unusual about
    what there is.
    """
    start, end = period if period else (None, None)
    prev_start, prev_end = previous_period if previous_period else (None, None)
    if not (start and end and prev_start and prev_end):
        return None

    this_months = period_module.approx_months(start, end)
    prev_months = period_module.approx_months(prev_start, prev_end)
    if this_months == prev_months:
        return None

    return (f"The current period runs from {start.strftime('%d %B %Y')} to "
           f"{end.strftime('%d %B %Y')} (approximately {this_months} "
           f"months), while the comparative period runs from "
           f"{prev_start.strftime('%d %B %Y')} to "
           f"{prev_end.strftime('%d %B %Y')} (approximately {prev_months} "
           f"months). As the two periods are not of the same length, the "
           f"amounts presented for the current and comparative periods are "
           f"not directly comparable.")


def _assemble_v2_note(note, financial_year, first_year=False, period=None,
                      previous_period=None):
    """A library 2.x note, built from the library's own conditions.

    Every paragraph is decided by services/conditions.py. What prints is the
    library's approved wording; what is waiting on the preparer is kept on
    the section - `awaiting` holds the note incomplete, `offered` stays out
    unless someone adds it - and never printed as a guess.
    """
    from . import bindings, conditions

    figures = bindings.figures_for(financial_year)
    html_parts, table_specs, awaiting, offered = [], [], [], []
    placed = set()
    # What the preparer already decided about the paragraphs no balance can
    # settle ("Preparer confirms the Company renders services"): kept per
    # engagement so a rebuilt note does not ask the same question again.
    from ..models import DocumentFigure
    decisions = {row.field: (row.text or "") for row in
                 DocumentFigure.query.filter_by(
                     financial_year_id=financial_year.id,
                     token="CONFIRM").all()}
    tables = {p["table_id"] for p in _all_pieces(note)
              if p.get("table_id") and p.get("rows")}

    def run(owner, heading=None):
        parts = []
        for piece in owner.get("pieces") or []:
            if piece.get("table_id"):
                continue                   # placed by its [table id] paragraph
            action, reason = conditions.paragraph(
                piece, owner.get("library_code"), figures)
            if action == conditions.SKIP:
                continue
            if action in (conditions.HOLD, conditions.OMIT):
                verdict = decisions.get(piece.get("para_id") or "")
                if verdict == "no":
                    continue                     # does not apply: left out
                if verdict == "yes":
                    action = conditions.PRINT    # applies: printed
            if action in (conditions.HOLD, conditions.OMIT):
                (awaiting if action == conditions.HOLD else offered).append({
                    "para_id": piece.get("para_id"),
                    "heading": heading,
                    "question": piece.get("condition_text") or "",
                    "reason": reason,
                    "wording": piece.get("wording") or "",
                    "tag": piece.get("tag"),
                })
                continue
            wording = piece.get("wording") or ""
            match = TABLE_PLACEHOLDER.match(wording)
            if match:
                table_id = match.group(1).strip()
                if table_id in tables and table_id not in placed:
                    placed.add(table_id)
                    table_specs.append({"source": "bindings",
                                        "version_id": note.get("library_version_id"),
                                        "table_id": table_id,
                                        "note_code": note.get("library_code")})
                continue
            if wording.strip():
                # The paragraph id travels with the sentence. An override on
                # the wording (library 3.5, OV-02) is addressed by it, so the
                # edit stays put when the note is reordered or rebuilt.
                para_id = piece.get("para_id") or ""
                mark = f' data-para="{para_id}"' if para_id else ""
                parts.append(f"<p{mark}>{wording}</p>")
        return parts

    html_parts.extend(run(note))

    for sub in note.get("subsections") or []:
        if sub.get("key") == "comparative_information" and first_year:
            html_parts.append(f"<h4>{sub['heading']}</h4>")
            html_parts.append(f"<p>{_first_period_wording(period)}</p>")
            continue
        on, _reason = conditions.note_applies(sub, figures, financial_year)
        if not on:
            continue
        parts = run(sub, heading=sub.get("heading"))
        if sub.get("key") == "comparative_information":
            mismatch = _comparative_length_note(period, previous_period)
            if mismatch:
                parts.insert(0, f"<p>{mismatch}</p>")
        if parts:
            # Never an empty heading: a sub-section with nothing printing
            # under it is left out whole, as the library says.
            html_parts.append(f"<h4>{sub['heading']}</h4>")
            html_parts.extend(parts)

    return "\n".join(html_parts), table_specs, {"awaiting": awaiting,
                                                "offered": offered}


def _operating_expense_keys():
    """Every standard line that sits in the statement's operating expenses."""
    from .classify import _index

    return sorted(key for key, entry in _index().items()
                  if entry.get("group") == "operating_expenses")


def _assemble_note_content(note, present, first_year=False, period=None,
                           previous_period=None, financial_year=None):
    """Build a note's starting text and figure tables from whichever of its
    pieces are triggered right now.

    This runs once, when the report is first created - the same point
    hand-authored content used to be copied in from report_sections.yaml.
    Like that content, what is produced here is then auditor-editable and
    frozen; it is not silently regenerated on every render, so an auditor's
    edit is never overwritten by a later trigger recalculation.

    Returns (html, table specs, drafts). A library 2.x note, given the
    engagement, is built by _assemble_v2_note instead.
    """
    from . import conditions

    if financial_year is not None and conditions.is_v2(note):
        html, specs, _asked = _assemble_v2_note(
            note, financial_year, first_year=first_year, period=period,
            previous_period=previous_period)
        return html, specs, []

    html_parts = []
    table_specs = []
    seen_table_keys = set()
    drafts = []

    # Library 2.x: every table is placed by a paragraph whose whole text is
    # "[table <id>]", and each of its rows names its own figure. Such a note
    # builds its tables from those bindings, never from a flat account list,
    # and the placing paragraph is an instruction, not wording to print.
    version_id = note.get("library_version_id")
    bound_tables = {p["table_id"] for p in _all_pieces(note)
                    if p.get("table_id") and p.get("rows")}

    def place_table(piece):
        """True if this piece places a bound table (added or deliberately not)."""
        if not bound_tables:
            return False
        if piece.get("table_id") in bound_tables:
            return True                     # placed by its paragraph instead
        match = TABLE_PLACEHOLDER.match(piece.get("wording") or "")
        if not match:
            return False
        table_id = match.group(1).strip()
        # A plain TABLE paragraph places its table whenever the note is in;
        # the table itself decides whether it has anything to show. A TOGGLE
        # or MANUAL one asks a question first, and until it is answered the
        # table stays out rather than being assumed.
        if (piece.get("tag") == "TABLE"
                or _piece_triggered(piece.get("tick_state"),
                                    piece.get("tb_keys"), present)):
            if table_id in bound_tables and table_id not in seen_table_keys:
                seen_table_keys.add(table_id)
                table_specs.append({"source": "bindings",
                                    "version_id": version_id,
                                    "table_id": table_id})
        return True

    def held_back(piece, heading=None):
        """True if this piece is drafted wording nobody has reviewed yet.

        68 paragraphs in the library were written for it rather than taken
        from the disclosure index. They are plausible and they are not
        approved, and that difference becomes invisible the moment wording
        sits in a note looking like every other sentence in the accounts.

        So they are not assembled at all. The note is built from reviewed
        wording only, and the draft is handed to the preparer through
        content_gaps() to read, judge and write in themselves. Any sentence
        that reaches a client's financial statements is then one a person
        put there - the same rule the figures already follow.
        """
        # Wording with a blank nobody has filled - "{ITEM} of ${AMOUNT}" - is
        # held back for the same reason as unreviewed wording, and more
        # plainly: it is not a sentence yet. Printed as it stood, the raw
        # braces reached the delivered accounts. Held, the preparer is shown
        # it as a draft to complete or drop.
        if (piece.get("review_status") != "unreviewed"
                and not _OPEN_BLANK.search(piece.get("wording") or "")):
            return False
        if piece.get("wording"):
            drafts.append({
                "heading": heading,
                "wording": piece["wording"],
                "requirement": piece.get("requirement") or "",
                "ref": piece.get("ref") or "",
            })
        return True

    def add_piece(piece):
        # Checked before the trigger, not after: a MANUAL piece never
        # reaches "triggered" at all (see _piece_triggered), so checking
        # trigger first meant an unreviewed piece that was always going to
        # need a person's confirmation never got its draft captured either -
        # invisible, not just correctly un-printed. held_back() only cares
        # about review status, never about whether the piece would have
        # fired, so it belongs first regardless of what follows.
        if held_back(piece):
            return
        if place_table(piece):
            return
        if not _piece_triggered(piece.get("tick_state"), piece.get("tb_keys"),
                                present):
            return
        if piece.get("output_form") == "Narrative paragraph":
            wording = piece.get("wording")
            if wording:
                html_parts.append(f"<p>{wording}</p>")
        elif piece.get("output_form") in TABLE_FORMS:
            keys = piece.get("tb_keys") or []
            signed = False
            if note.get("key") == "administrative_and_other_expenses":
                # The library asks this note for depreciation and staff
                # costs, so it listed two accounts' worth of a 696,122
                # expense block. The note explains the operating expenses
                # line, so it lists everything in that line, signed so the
                # income items inside it net off and the total ties to it.
                keys = _operating_expense_keys()
                signed = True
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
                # A "|"-separated wording is the library's list of the
                # columns or rows a table might hold ("Carrying amount |
                # Fair value | Level within the fair value hierarchy"), an
                # instruction to the preparer. Printed over the table it read
                # as a caption nobody wrote.
                if "|" in heading:
                    heading = None
                # An empty string here still satisfies "total" in spec (it
                # is a str, just a blank one - see the label line in
                # notes.py), so the footed row printed with two bare
                # figures and nothing in front of them. Auditor-editable
                # afterwards like the rest of this table, same as a
                # statement's own "Total ..." rows.
                spec = {"source": "accounts", "keys": keys,
                        "heading": heading, "total": "Total"}
                if signed:
                    spec["signed"] = True
                table_specs.append(spec)
            # No resolvable trial balance keys: nothing to compute, so
            # nothing is added. The auditor adds it by hand if it applies -
            # see readiness.py for the equivalent "flag, don't fabricate"
            # rule on the missing-documents side.

    for piece in note.get("pieces", []):
        add_piece(piece)

    for sub in note.get("subsections", []):
        sub_parts = []

        # A first period since incorporation. The library's own comparative
        # wording covers reclassified comparatives, which cannot apply when
        # there are no comparatives at all - printing it would state
        # something untrue about the accounts. The first-year counterpart is
        # substituted instead, and it names the actual period, because
        # whether that period is twelve months is the whole point of saying
        # it. Auditor-editable afterwards like any other note text.
        if sub.get("key") == "comparative_information" and first_year:
            sub_parts.append(f"<p>{_first_period_wording(period)}</p>")
        else:
            # A period whose length differs from the comparative period's
            # is flagged whenever the previous year is linked, regardless
            # of this subsection's own manual tick state - that gate
            # decides whether the RECLASSIFICATION wording below appears,
            # not whether a genuine difference in period length gets said.
            if sub.get("key") == "comparative_information":
                mismatch = _comparative_length_note(period, previous_period)
                if mismatch:
                    sub_parts.append(f"<p>{mismatch}</p>")

            # Same reordering as add_piece() above, for the same reason: a
            # sub-section that never triggers (or a piece inside one that
            # never does) still deserves its unreviewed wording captured as
            # a draft, not silently dropped along with the printing decision
            # that correctly keeps it off the page.
            sub_triggered = _piece_triggered(sub.get("tick_state"),
                                             sub.get("trigger_keys"), present)
            for piece in sub.get("pieces", []):
                if held_back(piece, heading=sub.get("heading")):
                    continue
                if not sub_triggered:
                    continue
                if place_table(piece):
                    continue
                if not _piece_triggered(piece.get("tick_state"),
                                        piece.get("tb_keys"), present):
                    continue
                if (piece.get("output_form") == "Narrative paragraph"
                        and piece.get("wording")):
                    sub_parts.append(f"<p>{piece['wording']}</p>")

        if sub_parts:
            html_parts.append(f"<h4>{sub['heading']}</h4>")
            html_parts.extend(sub_parts)

    # A note whose only requirement is a figure - "Administrative and other
    # expenses" asks for nothing but the depreciation/staff-cost breakdown,
    # FRS 107's amortised-cost note is nothing but a table - is already
    # complete once that table is built. Printing "Write this note here."
    # above a table that already answers the requirement told the preparer
    # something was missing when it was not, and printed those words into
    # delivered accounts next to a fully populated table.
    if not html_parts and not table_specs:
        html_parts.append(UNWRITTEN_NOTE_HTML)

    return "\n".join(html_parts), table_specs, drafts


def _build_note_section(note, present, sort_order, report_id,
                        first_year=False, period=None, previous_period=None,
                        financial_year=None):
    from . import bindings, conditions

    asked = None
    if financial_year is not None and conditions.is_v2(note):
        content_html, table_specs, asked = _assemble_v2_note(
            note, financial_year, first_year=first_year, period=period,
            previous_period=previous_period)
        drafts = []
        enabled, _reason = conditions.note_applies(
            note, bindings.figures_for(financial_year), financial_year)
        # On, but nothing in it applies and nothing is waiting: the library
        # suppresses a note with no content rather than print its heading.
        if enabled and not (content_html.strip() or table_specs
                            or asked["awaiting"]):
            enabled = False
    else:
        content_html, table_specs, drafts = _assemble_note_content(
            note, present, first_year=first_year, period=period,
            previous_period=previous_period)
        enabled = _note_triggered(note, present)

    binding = {}
    if asked and asked["awaiting"]:
        # Held: the note is incomplete until the preparer answers these.
        binding["awaiting_preparer"] = asked["awaiting"]
    if asked and asked["offered"]:
        # Left out unless the preparer says they apply.
        binding["offered_to_preparer"] = asked["offered"]
    if table_specs:
        binding["note_table_specs"] = table_specs
    if drafts:
        # Kept on the section rather than recomputed on each builder load, so
        # the preparer still sees what the library drafted even after writing
        # the note in their own words.
        binding["draft_wording"] = drafts

    return AuditReportSection(
        report_id=report_id,
        section_key=f"{NOTE_PREFIX}{note['key']}",
        title=note["heading"],
        section_type="free_text",
        sort_order=sort_order,
        is_enabled=enabled,
        content_html=content_html,
        data_binding=binding or None,
    )


def rebuild_note_sections(report, financial_year):
    """Replace a report's notes with ones assembled from its current library.

    For an engagement moved to another library version. Every note section is
    rebuilt, and figure edits made against the old notes are removed with
    them - their rows no longer exist. Non-note sections (cover, directors'
    statement, the statements) are untouched. Returns (removed, added).
    """
    from ..models import ReportFigureOverride

    notes = [s for s in report.sections if s.section_key.startswith(NOTE_PREFIX)]
    if notes:
        ReportFigureOverride.query.filter(
            ReportFigureOverride.report_id == report.id,
            ReportFigureOverride.section_key.in_([s.section_key for s in notes]),
        ).delete(synchronize_session=False)
    for section in notes:
        report.sections.remove(section)
        db.session.delete(section)
    db.session.flush()

    order = max((s.sort_order for s in report.sections), default=-1) + 1
    present = _present_keys(financial_year)
    period = (financial_year.start_date, financial_year.end_date)
    previous_period = _previous_period_of(financial_year)
    added = 0
    for note in load_notes_catalogue(financial_year):
        db.session.add(_build_note_section(
            note, present, order, report.id,
            first_year=bool(financial_year.is_first_year), period=period,
            previous_period=previous_period, financial_year=financial_year))
        order += 1
        added += 1
    db.session.commit()
    return len(notes), added


def prior_year_wording(financial_year):
    """Last year's note wording, keyed by the library note it matched.

    Read out of the signed accounts by the extraction layer (point 2). Only
    matched notes are returned here: an unmatched note has no section to be
    carried into, and is surfaced on the document review screen instead so it
    is seen rather than lost.
    """
    out = {}
    for note in (PriorYearNote.query
                 .filter_by(financial_year_id=financial_year.id)
                 .order_by(PriorYearNote.id).all()):
        if note.matched_key and note.body_text:
            out.setdefault(note.matched_key, note)
    return out


_UPDATE_MARK = re.compile(r"\[update:[^\]]*\]")

# A figure quoted in last year's sentences: "S$65,560 (2022: S$78,300)", or a
# comparative in brackets: "(2022: 17%)", "(2022: 30-60 days terms)".
_STALE_FIGURE = re.compile(
    r"S\$\s?\d[\d,]*(?:\.\d+)?(?:\s*\(20\d\d:\s*S\$\s?\d[\d,]*(?:\.\d+)?\))?"
    r"|\(20\d\d:[^)]*\)")


def roll_forward_text(text, financial_year):
    """Last year's wording, brought up to this year's dates.

    The signed accounts' sentences quote last year's facts. Carried over as
    they stood they printed "31 DECEMBER 2024" in the 2025 accounts, and last
    year's amounts as if they were this year's. Two different things:

    A DATE is mechanical: last year's year end becomes this year's, in the
    case the sentence used.

    A FIGURE is not. What last year said about its receivables says nothing
    about this year's, and quietly keeping it would state a fact about the
    wrong year. Each is wrapped as "[update: S$65,560 (2022: S$78,300)]" so it
    is visible where it sits and the note stays Incomplete until somebody
    types this year's, or deletes the sentence.
    """
    from datetime import date

    if not text:
        return text
    end = getattr(financial_year, "end_date", None)
    if end:
        try:
            previous = date(end.year - 1, end.month, min(end.day, 28)
                            if end.month == 2 else end.day)
        except ValueError:
            previous = None
        if previous:
            pattern = re.compile(
                r"\b%d\s+%s\s+%d\b" % (previous.day,
                                       previous.strftime("%B"), previous.year),
                re.IGNORECASE)

            def move(match):
                new = f"{end.day} {end.strftime('%B')} {end.year}"
                return new.upper() if match.group(0).isupper() else new

            text = pattern.sub(move, text)
            # "... ended 31 DECEMBER 2025 and 31 December 2025": the template
            # named the same year end twice, the second being the comparative;
            # and "unchanged from <date>" means the previous year end.
            now = f"{end.day} {end.strftime('%B')} {end.year}"
            before = f"{previous.day} {previous.strftime('%B')} {previous.year}"
            text = re.sub(
                r"(?i)(\bended\s+%s)(\s+and\s+)%s\b" % (re.escape(now), re.escape(now)),
                lambda m: m.group(1) + m.group(2) + before, text)
            text = re.sub(
                r"(?i)(\b(?:remains?\s+)?unchanged\s+from\s+)%s\b" % re.escape(now),
                lambda m: m.group(1) + before, text)
        # "effective for annual periods beginning on or after 1 January 2023"
        # in a set for a later year: the sentence is about this year's periods
        text = re.sub(
            r"(?i)(beginning\s+on\s+or\s+after\s+1\s+January\s+)(20\d\d)\b",
            lambda m: m.group(1) + str(end.year) if int(m.group(2)) < end.year else m.group(0),
            text)
    return _STALE_FIGURE.sub(lambda m: f"[update: {m.group(0)}]", text)


def _note_reference_map(financial_year, report):
    """{last year's note number: this year's}, for the notes both years have."""
    from ..models import PriorYearNote

    numbers = note_number_map(report)
    mapped = {}
    for prior in PriorYearNote.query.filter_by(
            financial_year_id=financial_year.id).all():
        if prior.note_number and prior.matched_key:
            current = numbers.get(f"{NOTE_PREFIX}{prior.matched_key}")
            if current:
                mapped[str(prior.note_number).strip()] = str(current)
    return mapped


# "(Note 8)", "refer to Note 12" - but not the risk note's own headings, which
# read "Note 2. Significant increase in credit risk".
_NOTE_REF = re.compile(r"\bNote\s+(\d+)\b(?!\.)")


def roll_forward_references(html, financial_year, section):
    """Point last year's "(Note 8)" at the note that now holds that subject."""
    if not html or "Note " not in html:
        return html
    try:
        report = db.session.get(AuditReport, section.report_id)
        mapped = _note_reference_map(financial_year, report)
    except Exception:                                      # noqa: BLE001
        log.exception("Could not map note references")
        return html
    if not mapped:
        return html
    # A note that numbers ITS OWN sub-headings ("Note 4. Write off", the credit
    # risk note) refers to those, not to a note of the accounts: "(Note 4)"
    # there means its own fourth heading, and was being renumbered to the
    # revenue note.
    own = set(re.findall(r"<h4>\s*Note\s+(\d+)\.", html))
    return _NOTE_REF.sub(
        lambda m: ("Note " + m.group(1) if m.group(1) in own
                   else f"Note {mapped.get(m.group(1), m.group(1))}"), html)


_NUMBERED_HEADING = re.compile(r"^\d+(\.\d+)*\.?\s+\S")


_GRADING_BASIS = re.compile(
    r"^(?P<cat>\d+)\s+(?P<desc>.+?)\s+"
    r"(?P<basis>12-months? ECL|Lifetime ECL|Written off|Difference between.+)$",
    re.IGNORECASE)


def _grading_table(lines):
    """The credit risk grading table as last year's accounts printed it.

    Category, Description, Basis of recognising ECL - words, no amounts. Kept
    as the table it was, not joined into one line; and it is the reason the
    library's amounts table for the same subject can be left out.
    """
    from html import escape

    if len(lines) < 3 or not re.match(r"^category\b", lines[0], re.IGNORECASE):
        return None
    rows = []
    for line in lines[1:]:
        match = _GRADING_BASIS.match(line.strip())
        if not match:
            return None
        rows.append(match.group("cat", "desc", "basis"))
    head = ("<thead><tr><th>Category</th><th>Description</th>"
            "<th>Basis of recognising ECL</th></tr></thead>")
    body = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % tuple(
            escape(cell) for cell in row) for row in rows)
    return f'<table class="fin note-table grading">{head}<tbody>{body}</tbody></table>'


def _carried_html(prior, financial_year):
    """Last year's note, ready to sit in this year's report.

    Dates rolled, figures marked for update, paragraphs and headings restored,
    and the table of new accounting standards put back where the first reading
    of the template lost it. Row-label lists are dropped only in a note whose
    template counterpart printed figures (the real table prints from the
    books); in a note of words they are content, and stay as a list.
    """
    from . import template_follow, template_structure, template_tables

    counts = template_follow._figure_notes(financial_year) or {}
    drop = bool(counts.get(str(prior.note_number or "")))
    body = prior.body_text
    # The wording is first read through an AI service that does not always keep
    # its paragraph breaks. Flat, it printed as one block with "3.1 Critical
    # judgements ..." inside the sentences: read the same note's structure from
    # the template PDF instead, which has it.
    if template_structure.is_flat(body) and prior.note_number:
        path = template_follow._template_path(financial_year)
        structured = (template_structure.read_notes(path) or {}) if path else {}
        rebuilt = structured.get(str(prior.note_number).strip())
        if rebuilt and len(rebuilt) > 0.6 * len(body or ""):
            body = rebuilt
    html = prior_text_to_html(roll_forward_text(body, financial_year),
                              drop_labels=drop)
    template_path = _template_pdf_path(financial_year)
    html = template_tables.restore_standards_table(html, template_path)
    return template_tables.restore_grading_table(html, template_path)


def prior_text_to_html(text, drop_labels=True):
    """Last year's plain-text note as paragraphs and headings.

    The signed accounts' note is stored as plain text - blank lines between
    paragraphs, a heading on a line of its own ("2.1 Basis of preparation",
    "Financial assets"). Put into a note as it stood, HTML collapsed every line
    break and the whole policies note printed as one run-on paragraph.

    Two kinds of short line are NOT headings. A block of several short lines
    with no full stop, or a "Total ..." line, is the row labels of a table whose
    figures were left behind in the PDF - "Trade receivables / - Third parties
    / Other receivables"; this year's table prints from the books, so those
    labels are dropped rather than printed as loose text. And a short line is a
    heading only when a paragraph follows it, not when it is the last of a run
    of labels.
    """
    from html import escape

    def ends_sentence(line):
        return bool(re.search(r"[.;]$", line))

    blocks = []
    # Word's symbol-font bullets come through the PDF as private-use
    # characters, which print as an empty box in any other font.
    text = re.sub("[]", "•", text or "")
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if len(lines) > 1 and all(len(ln) <= 90 for ln in lines) and not any(
                ends_sentence(ln) for ln in lines):
            blocks.append(("labels", lines))          # a table's row labels
        elif len(lines) == 1 and len(lines[0]) <= 90 and not re.search(
                r"[.;,:]$", lines[0]):
            if re.match(r"^total\b", lines[0], re.I):
                blocks.append(("labels", lines))
            elif _NUMBERED_HEADING.match(lines[0]):
                blocks.append(("heading", lines))
            else:
                blocks.append(("candidate", lines))
        else:
            blocks.append(("para", lines))

    out = []
    for index, (kind, lines) in enumerate(blocks):
        if kind == "labels":
            if not drop_labels:
                out.append("<p>" + "<br>".join(escape(ln) for ln in lines) + "</p>")
            continue
        if kind in ("heading", "candidate"):
            if kind == "candidate":
                # A heading only if a real paragraph follows the run of
                # candidates; otherwise it is the tail of a label list.
                nxt = next(((k, ln) for k, ln in blocks[index + 1:]
                            if k not in ("candidate", "heading")), None)
                if not nxt or nxt[0] != "para" or len(" ".join(nxt[1])) < 100:
                    continue
            out.append(f"<h4>{escape(lines[0])}</h4>")
            continue
        grading = _grading_table(lines)
        if grading:
            out.append(grading)
        elif any(re.match(r"^[•\-]\s*", ln) for ln in lines):
            out.append("<p>" + "<br>".join(escape(ln) for ln in lines) + "</p>")
        else:
            out.append("<p>" + escape(" ".join(lines)) + "</p>")
    return "\n".join(out)


def carry_forward_prior_wording(report, financial_year) -> int:
    """Offer last year's sentences as the starting text of this year's notes.

    Runs on every builder load and is idempotent. Returns how many sections
    were filled this time.

    The rule that matters: it only ever writes into a note still holding the
    library's untouched default. The moment a preparer types anything, that
    note is theirs and this leaves it alone forever - carrying last year's
    wording over an auditor's own words would be destroying work, and doing
    it silently on a page load would be worse.

    Nor does it enable anything. Whether a note appears is decided by whether
    the figure it explains is present, which is a question about THIS year;
    last year's disclosure has no vote. See `prior_year_disclosed`.
    """
    wording = prior_year_wording(financial_year)
    if not wording:
        return 0

    present = _present_keys(financial_year)
    catalogue = {n["key"]: n for n in load_notes_catalogue(financial_year)}
    filled = 0

    for section in report.sections:
        if not section.section_key.startswith(NOTE_PREFIX):
            continue
        key = section.section_key[len(NOTE_PREFIX):]
        if section.prior_note_id:                  # already carried
            # Carried as raw text by an earlier version, and not touched
            # since: put it into paragraphs and headings. Anything a person
            # has edited no longer equals the raw text and is left alone.
            carried = wording.get(key)
            raw = (carried.body_text or "").strip() if carried else ""
            if (carried is not None and carried.id == section.prior_note_id
                    and raw and (section.content_html or "").strip() == raw):
                section.content_html = _carried_html(carried, financial_year)
                filled += 1
            elif (carried is not None and carried.id == section.prior_note_id
                    and raw):
                # Stored flat (no paragraph breaks) and carried into the note as
                # one block: rebuilt from the template's own structure, but only
                # if nobody has touched it since - the block must be exactly what
                # the flat wording converts to.
                from . import template_structure
                if template_structure.is_flat(carried.body_text):
                    as_carried = prior_text_to_html(
                        roll_forward_text(carried.body_text, financial_year))
                    if (section.content_html or "").strip() == as_carried.strip():
                        rebuilt = _carried_html(carried, financial_year)
                        if rebuilt.strip() != as_carried.strip():
                            section.content_html = rebuilt
                            filled += 1
            continue

        note = catalogue.get(key)
        prior = wording.get(key)
        if not note or prior is None:
            continue

        # Untouched means identical to what the library would generate right
        # now. Recomputed rather than remembered, so a section is correctly
        # treated as edited even if it was changed before this existed.
        default_html, _specs, _drafts = _assemble_note_content(
            note, present, first_year=bool(financial_year.is_first_year),
            period=(financial_year.start_date, financial_year.end_date),
            previous_period=_previous_period_of(financial_year),
            financial_year=financial_year)
        current = (section.content_html or "").strip()
        if current and current != (default_html or "").strip():
            continue

        carried_html = _carried_html(prior, financial_year)
        if not _has_narrative(carried_html):
            # The template's note was a table and nothing else (revenue, finance
            # cost). That is not the library missing: it holds the note's own
            # wording, and a note with a table and no sentence about it reads
            # as unfinished. The reviewed wording the library ticks "always"
            # (or ticks for the accounts present) leads the table; what it
            # leaves to the preparer stays out.
            carried_html = _library_lead_in(note, present) + carried_html
        section.content_html = carried_html
        section.prior_note_id = prior.id
        filled += 1

    if filled:
        db.session.commit()
        log.info("FY %s: carried %d note(s) forward from last year's accounts",
                 financial_year.id, filled)
    return filled


def _template_pdf_path(financial_year):
    """The PDF this report follows: the customer's uploaded template, else last
    year's signed accounts on file - the same choice _match_customer_template
    makes. Tables lost in the text (the standards list, the credit risk grading)
    are read back from it, so it has to be the file the notes were read from."""
    from pathlib import Path

    from . import template_follow

    chosen = getattr(financial_year.customer, "report_template_path", None)
    if chosen and chosen != "STANDARD" and Path(str(chosen)).exists():
        return chosen
    path = template_follow._template_path(financial_year)
    if path is not None and Path(str(path)).exists():
        return str(path)
    for document in financial_year.documents:
        if (document.category == "signed_accounts"
                and Path(str(document.storage_path)).suffix.lower() == ".pdf"
                and Path(str(document.storage_path)).exists()):
            return str(document.storage_path)
    return chosen


def _has_narrative(html):
    """Whether carried note wording has a real sentence in it."""
    from html import unescape
    for paragraph in re.findall(r"<p[^>]*>(.*?)</p>", html or "", re.S):
        text = unescape(re.sub(r"<[^>]+>", "", paragraph)).strip()
        if len(text) >= 60 and " " in text:
            return True
    return False


def _library_lead_in(note, present):
    """The library's own sentences for a note, as paragraphs: reviewed, no table
    placeholder, no blank for a person to fill, ticked always or by the accounts."""
    from html import escape

    out = []
    for piece in _all_pieces(note):
        text = (piece.get("wording") or "").strip()
        if (not text or TABLE_PLACEHOLDER.match(text) or _OPEN_BLANK.search(text)
                or piece.get("review_status") == "unreviewed"):
            continue
        if not _piece_triggered(piece.get("tick_state"), piece.get("tb_keys"), present):
            continue
        out.append("<p>%s</p>" % escape(text))
    return "".join(out)


def prior_year_disclosed(financial_year) -> set:
    """Section keys whose note this company disclosed last year.

    Returned already prefixed so a caller can test `section.section_key in
    ...` directly, rather than re-deriving the prefix and getting it wrong.

    Shown against the tick list so the preparer can see what last year's
    accounts covered. Deliberately NOT wired into whether a note is ticked:
    a company that repaid its bank loan should not receive a borrowings note
    this year merely because it needed one last year. Last year informs the
    preparer; it does not decide the accounts.
    """
    return {f"{NOTE_PREFIX}{key}"
            for key in prior_year_wording(financial_year)}


def prior_notes_dropped(report, financial_year):
    """Notes disclosed last year that this year's accounts do not carry.

    The firm's rule, and it is the right one: a note that was in last year's
    accounts and is not in this year's is not necessarily wrong - companies
    repay loans and dispose of subsidiaries - but it must never happen
    silently. Last year the company told its readers something; dropping it
    without a word is the one outcome nobody can review.

    Returns a row per dropped note with the reason it is not there, so the
    preparer confirms the omission rather than discovering it after signing.
    """
    catalogue = {n["key"]: n for n in load_notes_catalogue(financial_year)}
    present = _present_keys(financial_year)
    sections = {s.section_key: s for s in report.sections}

    dropped = []
    for prior in (PriorYearNote.query
                  .filter_by(financial_year_id=financial_year.id)
                  .order_by(PriorYearNote.id).all()):

        # "2.2 Revenue recognition", "3.1 Critical judgements": headings
        # inside a policy note. The note they sit in is what this year's
        # accounts carry or drop; listing each as its own missing note buried
        # the two or three that had really gone under eleven that had not.
        if re.match(r"^\d+\.\d", str(prior.note_number or "")):
            continue

        label = prior.title
        if prior.note_number:
            label = f"{prior.note_number}. {prior.title}"

        # Nothing in the library matches it. The strongest case for saying
        # something: there is no note here to tick even if the preparer
        # wanted to, so silence would leave them no way to notice.
        if not prior.matched_key:
            dropped.append({
                "label": label,
                "reason": ("The FRS library has no note matching this "
                           "heading. If it still applies, add it as a note "
                           "of your own."),
                "severity": "missing",
            })
            continue

        section = sections.get(f"{NOTE_PREFIX}{prior.matched_key}")
        if section is None:
            dropped.append({
                "label": label,
                "reason": ("Last year's note has no counterpart in this "
                           "report."),
                "severity": "missing",
            })
            continue

        if section.is_enabled:
            continue                               # carried - nothing to say

        note = catalogue.get(prior.matched_key) or {}
        triggers = [k for k in (note.get("trigger_keys") or [])]
        tick_state = note.get("tick_state")

        from . import bindings, conditions

        if conditions.is_v2(note):
            on, why = conditions.note_applies(
                note, bindings.figures_for(financial_year), financial_year)
            reason = (f"The notes library leaves it out this year: {why}. "
                      f"If that is wrong, the account is missing or unmapped, "
                      f"or tick it by hand." if not on else
                      "Someone switched it off by hand this year. Confirm "
                      "that was intended before approving.")
        elif tick_state == "tb_driven" and triggers:
            missing_keys = ", ".join(triggers)
            reason = (f"Switched off because this year's trial balance has "
                      f"no {missing_keys}. If that is right, the disclosure "
                      f"ends with the balance; if not, the account is "
                      f"missing or unmapped.")
        elif tick_state == "manual":
            reason = ("A manual note - it is never switched on by itself. "
                      "Tick it if it still applies this year.")
        else:
            reason = ("This note is required for every engagement, but "
                      "someone switched it off by hand this year. Confirm "
                      "that was intended before approving.")

        dropped.append({"label": label, "reason": reason,
                        "severity": "off", "section_id": section.id})

    return dropped


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
        return (s.section_type != "statement" and not is_statutory(s)
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

    catalogue = {n["key"]: n for n in load_notes_catalogue(financial_year)}
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

            # Only a line that actually reaches the face can be missing a
            # note on it. Every statement line nil in BOTH years is now left
            # off (see reports/_document.html), not just the ones the
            # template marks optional - so asking for a note on one would
            # send the preparer looking for a disclosure to support a line
            # the reader never sees.
            line = lines_by_key.get(spec["key"])
            printing = bool(line and (line.effective_amount
                                      or line.amount_previous))
            if printing:
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

    # A first period needs wording the library has no note for. Rather than
    # invent it - the rule everywhere else here - it is named as a gap the
    # preparer fills, the same as any other.
    if financial_year.is_first_year:
        thin.append({
            "note": "Comparative information (first period)",
            "detail": ("This is the company's first financial period, so "
                       "there is no comparative column and the usual "
                       "comparative wording does not apply. State the period "
                       "covered — from incorporation to the year end, which "
                       "may be longer or shorter than twelve months — in the "
                       "corporate information note. The FRS library has no "
                       "note for this, so it is yours to write."),
        })

    # A note that is switched on but has nothing in it yet. Two ways that
    # happens: it was added by hand and still holds the prompt it was created
    # with, or it is a library note whose every content piece was left
    # untriggered. Both print in the delivered accounts as a numbered heading
    # with no disclosure under it - and the prompt prints the words "Write
    # this note here." into a client's financial statements.
    #
    # Reported rather than skipped at render time on purpose: note numbers
    # are recalculated from whichever notes are enabled, so quietly dropping
    # one here would renumber the rest and leave every "see Note N" on the
    # face of a statement pointing somewhere else.
    unwritten = []
    unwritten_section_ids = set()
    for section in ordered_sections(report):
        if not section.is_enabled or section.section_type == "statement":
            continue
        body = (section.content_html or "").strip()
        has_table = bool((section.data_binding or {}).get("note_table_specs"))
        waiting = bool((section.data_binding or {}).get("awaiting_preparer"))
        still_a_prompt = body in UNWRITTEN_NOTE_FORMS
        if not (still_a_prompt or (not body and not has_table)):
            continue
        if waiting and not still_a_prompt:
            continue                 # reported below as waiting, not unwritten
        unwritten_section_ids.add(section.id)
        unwritten.append({
            "note": section.title,
            "detail": ("This note is switched on but nothing has been written "
                       "in it yet. As it stands it prints as a heading with "
                       "no disclosure under it. Write it, or switch it off."),
        })

    # Wording the library drafted but nobody has reviewed. Held out of the
    # note itself by _assemble_note_content - see held_back() there - and
    # shown here instead, so the preparer can read what was drafted, decide
    # whether it is right for this company, and write it in themselves.
    #
    # Not a defect in the accounts like the categories above: a note can be
    # complete with none of this used. It is offered, and offering it is the
    # only safe place for wording that has not been signed off.
    #
    # Shown ONLY for a note that is still unwritten. A note that already has
    # real content - an auto-built table, reviewed paragraphs, or the
    # preparer's own words - does not need this draft to be readable right
    # now, and listing it anyway just makes the card longer for no reason:
    # every enabled note that happened to carry one unreviewed piece would
    # appear here regardless of whether the note itself needed anything.
    # A note whose only content IS the placeholder is the one case where the
    # draft is the most useful thing the preparer can be handed, so that is
    # the only case this surfaces it in.
    unreviewed = []
    for section in ordered_sections(report):
        if section.id not in unwritten_section_ids:
            continue
        for draft in (section.data_binding or {}).get("draft_wording", []):
            unreviewed.append({
                "note": section.title,
                "heading": draft.get("heading") or "",
                # Bindings resolved before the preparer reads it. The draft
                # is there to be judged and written in; showing them
                # "{{ firm.credit_terms_receivable }}" asks them to judge
                # template syntax instead of a sentence, and hides whether
                # the firm has actually answered it.
                "wording": render_bindings(draft.get("wording") or "",
                                           financial_year.customer,
                                           financial_year),
                "requirement": draft.get("requirement") or "",
                "ref": draft.get("ref") or "",
            })

    # Standing wording the firm has never answered. A note carrying an
    # unanswered blank prints "[credit terms receivable not set]" into the
    # accounts, which is as incomplete as a note nobody has written - so it
    # counts towards has_gaps, unlike the drafted-wording offer above.
    #
    # Only reported where a note that actually uses it is switched on: a
    # company with no receivables has no reason to be told the firm has not
    # set its receivable credit terms.
    from . import disclosure_settings

    # Both what is printing and what is offered as a draft: a blank the
    # preparer is about to paste in is exactly as unanswered as one already
    # in the note, and telling them afterwards is worse than telling them
    # while they are looking at it.
    live = " ".join(
        [(s.content_html or "") for s in ordered_sections(report)
         if s.is_enabled]
        + [d.get("wording") or ""
           for s in ordered_sections(report) if s.is_enabled
           for d in (s.data_binding or {}).get("draft_wording", [])])
    unanswered = [{"key": key, "label": label}
                  for key, label in disclosure_settings.unset_keys(
                      financial_year.customer)
                  if f"firm.{key}" in live]

    # Library 2.x paragraphs no document or balance can decide. AWAITING
    # holds its note incomplete until the preparer answers - the library's
    # "Hold". OFFERED stays out unless the preparer says it applies - its
    # "Omit" - and is listed so a paragraph never disappears unseen.
    awaiting, offered = [], []
    for section in ordered_sections(report):
        if not section.is_enabled:
            continue
        binding = section.data_binding or {}
        for kind, bucket in (("awaiting_preparer", awaiting),
                             ("offered_to_preparer", offered)):
            for item in binding.get(kind, []):
                bucket.append({
                    # Which section it belongs to, so the panel that lists
                    # it can send the preparer to the note rather than
                    # naming it and leaving them to find it.
                    "section_id": section.id,
                    "para_id": item.get("para_id") or "",
                    "note": section.title,
                    "heading": item.get("heading") or "",
                    "question": item.get("question") or "",
                    "reason": item.get("reason") or "",
                    "wording": render_bindings(item.get("wording") or "",
                                               financial_year.customer,
                                               financial_year),
                })

    return {"missing": grouped_missing, "thin": thin, "unwritten": unwritten,
            "unreviewed": unreviewed, "unanswered": unanswered,
            "awaiting": awaiting, "offered": offered,
            "has_gaps": bool(grouped_missing or thin or unwritten
                             or unanswered or awaiting)}


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
        # The customer's own lines, where the report was created from their
        # template and the statement is not the detailed breakdown.
        payload["presented"] = None
        cf_wording = (section.data_binding or {}).get("cash_flow_wording")
        if cf_wording and statement_type == "cash_flow":
            payload["headings"] = cf_wording["headings"]
            payload["cf_labels"] = cf_wording["labels"]
            if not payload["detailed"]:
                from . import template_statements
                try:
                    payload["cf_layout"] = template_statements.cash_flow_layout(
                        financial_year, cf_wording)
                except Exception:                          # noqa: BLE001
                    log.exception("Could not lay out the cash flow as the "
                                  "template does")
        lines = (section.data_binding or {}).get("presentation")
        if (lines and payload["statement"] is not None
                and payload["statement"].statement_type == "changes_in_equity"
                and lines[0].get("type") == "matrix"):
            from . import template_statements
            try:
                payload["matrix"] = template_statements.equity_matrix(
                    payload["statement"], financial_year,
                    lines[0].get("rows"))
            except Exception:                              # noqa: BLE001
                log.exception("Could not lay out the equity statement as "
                              "the template does")
            lines = None
        if lines and payload["statement"] is not None and not payload["detailed"]:
            from . import template_statements
            try:
                payload["presented"] = template_statements.present(
                    payload["statement"], lines)
                if payload["presented"]:
                    _drop_borrowed_refs(section, payload["presented"], lines)
                    _order_by_note(section, payload["presented"])
                if (payload["presented"] and payload["statement"].statement_type
                        == "balance_sheet"):
                    payload["headings"] = template_statements.group_headings(
                        (section.data_binding or {}).get("headings"),
                        payload["presented"])
            except Exception:                              # noqa: BLE001
                log.exception("Could not draw %s in the template's lines",
                              section.section_key)
    else:
        payload["html"] = render_bindings(section.content_html or "",
                                          customer, financial_year,
                                          chips=chips)
        if section.prior_note_id:
            payload["html"] = roll_forward_references(
                payload["html"], financial_year, section)
        note_table_spec = spec.get("note_table")
        if note_table_spec is None and section.data_binding:
            note_table_spec = section.data_binding.get("note_table_specs")
        payload["tables"] = notes_service.build_tables(
            note_table_spec, financial_year)
        if any(s.get("source") == "template" for s in (note_table_spec or [])
               if isinstance(s, dict)):
            from . import template_note_tables
            payload["html"] = template_note_tables.strip_table_labels(
                payload["html"], note_table_spec)
        # A row label is wording, and one of them carries a firm setting:
        # the credit risk gradings table names a category as "more than 30
        # days past due". Substituted here, where every other piece of
        # wording on the page is, so a placeholder can never reach a
        # client's accounts looking like template code.
        for table in payload["tables"]:
            for row in table.get("rows") or []:
                if row.get("label") and "{{" in row["label"]:
                    # Plain text: the label is printed escaped, and a blank
                    # rendered as a marked-up span printed its own tags
                    # ("<span class=missing-binding>") in a client's accounts.
                    row["label"] = re.sub(
                        r"<[^>]+>", "",
                        render_bindings(row["label"], customer,
                                        financial_year))
        apply_note_overrides(section, payload["tables"])
        payload["incomplete"] = incomplete_reasons(section, payload,
                                                   financial_year)
        # The same reasons in two short lines: what is missing, and which
        # document or answer settles it. The full list stays available.
        from . import completion_needs
        payload["incomplete_summary"] = completion_needs.summarise(
            payload["incomplete"])
        # The paragraphs waiting on a yes or no, shown in the note itself.
        binding = section.data_binding or {}
        payload["confirm"] = [
            {"section_id": section.id, "para_id": item.get("para_id") or "",
             "question": item.get("question") or "",
             "optional": kind == "offered_to_preparer",
             "wording": render_bindings(item.get("wording") or "",
                                        customer, financial_year)}
            for kind in ("awaiting_preparer", "offered_to_preparer")
            for item in binding.get(kind, []) if item.get("para_id")]

    return payload


_MISSING_BLANK = re.compile(r'class="[^"]*missing-binding[^"]*"[^>]*>([^<]+)<')


def _order_by_note(section, presented):
    """List a statement's numbered lines in the order of their note numbers.

    When the preparer moves a note the statement's note column stops reading
    downward ("5, 4, 6"). The numbered lines take one another's places so it does:
    the line whose note is 4 stands above the line whose note is 5. Only lines that
    carry a note number move, only among the places numbered lines already hold,
    and only inside their own group - totals, subtotals and unnumbered lines stay
    where they are.
    """
    numbers = note_number_map(section.report)

    def number(line):
        ref = getattr(line, "note_ref", None)
        text = numbers.get(ref) if ref else None
        return int(text) if text and str(text).isdigit() else None

    start = 0
    while start < len(presented):
        end = start
        while (end < len(presented)
               and presented[end].group_key == presented[start].group_key):
            end += 1
        slots = [i for i in range(start, end)
                 if not presented[i].is_total and not presented[i].is_subtotal
                 and number(presented[i]) is not None]
        ranked = sorted((presented[i] for i in slots), key=number)
        for slot, line in zip(slots, ranked):
            presented[slot] = line
        start = end


def _drop_borrowed_refs(section, presented, rows):
    """A line the template never had does not borrow another line's note number.

    "Short-term borrowings" is not in last year's balance sheet, and the note it
    would point to - the template's own "Loan from a bank" - says nothing about
    it, so two lines then carried the same number for figures only one of them
    has in its note. The line stays, without a reference, until a note of its own
    exists (a new note the preparer switches on carries its own number).
    """
    cover = next((s for s in section.report.sections
                  if s.section_key == "cover_page"), None)
    skeleton = ((cover.data_binding or {}).get("skeleton") if cover else None)
    if not skeleton:
        return
    owned = set()
    for entry in skeleton.get("template") or []:
        key = entry.get("key") or ""
        owned.add(key)
        owned.add(key[len(NOTE_PREFIX):] if key.startswith(NOTE_PREFIX) else key)
    types = {r["type"] for r in rows} | {"loan"}
    if "income_tax" in types:
        types.add("tax_expense")          # the template's "Income tax expenses"
    for line in presented:
        if line.line_key in types or line.is_total or line.is_subtotal:
            continue
        if line.note_ref and line.note_ref in owned:
            line.note_ref = None


def incomplete_reasons(section, payload, financial_year=None):
    """Why a section cannot be issued yet, in words. Empty when it can.

    Three things hold a note incomplete, all from the library's own rules:
    a question the preparer has not answered, a figure whose source is
    missing, and a blank in the wording nobody has filled. Worked out from
    what is actually rendered, so it can never disagree with the page.
    """
    reasons = []
    for item in (section.data_binding or {}).get("awaiting_preparer", []):
        question = (item.get("question") or "").strip()
        if question.lower() in ("", "-", "always"):
            question = item.get("heading") or section.title
        reasons.append(f"Waiting for the preparer: {question}")

    for table in payload.get("tables") or []:
        for reason in (table.get("held_table") or []) + (table.get("flags") or []):
            if reason not in reasons:
                reasons.append(reason)
        for row in table.get("rows") or []:
            for column in ("held_current", "held_previous"):
                reason = row.get(column)
                if reason and reason not in reasons:
                    reasons.append(reason)

    # A balance with nowhere to print in this note. The library's own
    # completeness rule, and the one check the engine still performs -
    # it asks whether a line has a row, not whether figures add up.
    if financial_year is not None:
        from . import bindings

        specs = (section.data_binding or {}).get("note_table_specs") or []
        for reason in bindings.uncovered_lines(specs, financial_year):
            if reason not in reasons:
                reasons.append(reason)

    # A question off the library's Preparer inputs sheet that nobody has
    # answered, where the sheet says an unanswered one holds the note.
    # Matched by the note's own library code, because the sheet names its
    # note in words and a section knows itself by code.
    if financial_year is not None:
        from . import preparer_inputs as input_service

        for reason in input_service.holds_for_section(section,
                                                      financial_year):
            if reason not in reasons:
                reasons.append(reason)

    # Who the related parties are, which nothing in the books can say.
    # The library's KI-01 blocks these notes until a person has ruled on
    # every candidate, because treating silence as "not related" would
    # quietly drop a disclosure.
    if financial_year is not None and _is_related_party_note(section):
        from . import related_parties

        for reason in related_parties.holds(financial_year):
            if reason not in reasons:
                reasons.append(reason)

    for mark in _UPDATE_MARK.findall(payload.get("html") or ""):
        text = "Update last year's figure: " + mark[len("[update:"):-1].strip()
        if text not in reasons:
            reasons.append(text)

    for blank in _MISSING_BLANK.findall(payload.get("html") or ""):
        text = f"Not filled in: {blank.strip('[]')}"
        if text not in reasons:
            reasons.append(text)
    return reasons


# The notes the related party register decides. Matched on the library
# code rather than on our section key, because the key follows the
# library heading and the client has already renamed these twice.
RELATED_PARTY_NOTES = ("RELATED_PARTY", "KEY_MANAGEMENT")


def _is_related_party_note(section):
    binding = section.data_binding or {}
    codes = [spec.get("note_code") or "" for spec in
             (binding.get("note_table_specs") or [])]
    codes.append(binding.get("library_code") or "")
    haystack = " ".join(codes).upper() + " " + (section.section_key or "").upper()
    return any(name in haystack for name in RELATED_PARTY_NOTES)


# Rows that are the engine talking to the preparer, not lines of the
# accounts. "Movement not yet analysed" is the whole of the set: it is
# not an SFRS caption, no source states it, and it exists to say that
# the three sections do not account for the movement in cash.
#
# It was printing in the label column with a figure beside it, among the
# real captions, which is the one place it must not be. A reader cannot
# tell it from an account, and it would be read as one - a line of these
# accounts, in these accounts, that no document supports.
WORKING_NOTE_LINES = {"cf_unexplained"}


def is_working_note(line):
    """Whether this row is a working mark rather than a line of the accounts."""
    return getattr(line, "line_key", None) in WORKING_NOTE_LINES


def statement_blockers(financial_year):
    """Faults on the face of the statements that stop a clean copy.

    Completeness was read off the notes alone, so nothing examined the
    primary statements, and a cash flow that does not reconcile could
    reach an approved set. The remainder prints as its own line -
    "Movement not yet analysed" - and a plug figure in a client's
    accounts is not a presentation choice. It is a number nobody can
    support, sitting in a statement that claims to explain where the
    cash went.

    The line stays on the working draft, and so does its amount: the
    preparer needs to see how much is unexplained, and hiding it would
    make a set that silently does not add up. What changes is that a
    draft carrying one cannot be approved.

    Almost always the cause is a missing opening cash balance, which is
    last year's closing cash and comes from the signed prior year
    accounts - so the reason says that rather than only naming the gap.
    """
    blockers = []
    statement = FinancialStatement.query.filter_by(
        financial_year_id=financial_year.id,
        statement_type="cash_flow").first()
    if statement is None:
        return blockers

    # A cash flow drawn from the customer's template is the preparer's entry
    # (standard lines v8). It is held until entered and agreed to the balance
    # sheet, and never carries an engine plug.
    from . import cash_flow_entry
    try:
        reasons = cash_flow_entry.check(financial_year)
    except Exception:                                   # noqa: BLE001
        log.exception("Could not check the entered cash flow")
        reasons = None
    if reasons is not None:
        if reasons:
            blockers.append((statement.type_label, reasons))
        return blockers

    for line in statement.lines:
        if line.line_key != "cf_unexplained" or not line.effective_amount:
            continue
        reason = ("%s of the movement in cash is not explained by the "
                  "operating, investing and financing sections, and prints "
                  "as “Movement not yet analysed”."
                  % _plain_amount(line.effective_amount))
        # Name the likely cause rather than guessing at one. With no
        # opening cash every movement is measured against nothing, which
        # is a different problem from a statement that is merely missing
        # a line, and telling a preparer to go and find last year's
        # accounts when last year's accounts are already loaded wastes
        # their afternoon.
        opening = next((other.effective_amount for other in statement.lines
                        if other.line_key == "cf_opening_cash"), None)
        if not opening:
            reason += (" No opening cash balance is loaded, so every "
                       "movement is measured from nil - it is last year's "
                       "closing cash, from the signed prior year accounts.")
        else:
            reason += (" Opening cash is loaded, so the gap is a movement "
                       "no line accounts for - most often a fixed asset "
                       "bought or sold, which is read from the fixed asset "
                       "register.")
        blockers.append((statement.type_label, [reason]))
        break
    return blockers


def _plain_amount(value):
    """A figure for a sentence: thousands separated, brackets for negative."""
    from decimal import Decimal, InvalidOperation

    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    text = "{:,.2f}".format(abs(number))
    return "(%s)" % text if number < 0 else text


def statement_note_gaps(payloads):
    """Balance sheet lines that carry an amount, that the customer's template
    never had, and that no note explains.

    The template lists the lines it printed, and the notes it had for them. A
    line this year's accounts need that it never had (other payables, short-term
    borrowings) prints without a note reference, and the set quietly explains
    less than it shows. It is put to the preparer instead: add a note for it, or
    say none is needed.
    """
    found = []
    for payload in payloads:
        section = payload["section"]
        presented = payload.get("presented")
        binding = section.data_binding or {}
        rows = binding.get("presentation") or []
        if not presented or binding.get("statement_type") != "balance_sheet" or not rows:
            continue
        known = {r.get("type") for r in rows} | {"loan"}
        reasons = []
        for line in presented:
            if line.is_total or line.is_subtotal or line.line_key in known:
                continue
            if getattr(line, "note_ref", None):
                continue
            amounts = [a for a in (line.amount_current, line.amount_previous) if a]
            if not amounts:
                continue
            reasons.append(
                "\u201c%s\u201d (%s) is on the balance sheet but the customer's "
                "template has no note for it. Add a note for it, or confirm that "
                "none is needed." % (line.label, _plain_amount(
                    line.amount_current or line.amount_previous)))
        if reasons:
            found.append((section.title, reasons))
    return found


def record_completeness(report, payloads):
    """Remember how many sections are incomplete, for screens that list many
    engagements and cannot afford to render every report. Returns the list
    of (title, reasons) that are incomplete."""
    from datetime import datetime

    incomplete = [(p["section"].title, p["incomplete"])
                  for p in payloads if p.get("incomplete")]
    # The statements as well as the notes. A set whose cash flow does not
    # reconcile is not a finished set, however complete its notes are.
    incomplete += statement_blockers(report.financial_year)
    incomplete += statement_note_gaps(payloads)
    if report.incomplete_notes != len(incomplete):
        report.incomplete_notes = len(incomplete)
        report.completeness_checked_at = datetime.utcnow()
        db.session.commit()
    return incomplete


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

    A cleared override is skipped, not applied (library 3.5, OV-07): the
    source figure comes back and the record stays behind for the reviewer.
    A live one leaves `override_record` on the row - the source figure, the
    reason, who and when - which is what marks it where it prints (OV-05).
    """
    from ..models import ReportFigureOverride

    if not tables:
        return

    overrides = [o for o in ReportFigureOverride.query.filter_by(
        report_id=section.report_id, section_key=section.section_key).all()
        if o.is_live]
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

            row["override_record"] = {
                "source_amount": override.source_amount,
                "source_label": override.source_label,
                "source_name": override.source_name or "the source",
                "reason": override.reason,
                "who": override.who,
                "when": override.updated_at or override.created_at,
            }
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


def section_pages(html: str, base_url: str = None) -> dict:
    """{"sec-<key>": page number} - where each section really lands.

    The contents page cites these. In the PDF the CSS does it itself
    (target-counter); the preview and the Word file have no pagination of
    their own, so the layout is worked out once here and the numbers are
    written in. {} when WeasyPrint is absent or the layout fails - the
    contents page then falls back to the CSS / a Word field.
    """
    if not weasyprint_available():
        return {}
    try:
        import weasyprint
        document = weasyprint.HTML(string=html, base_url=base_url).render()
        found = {}
        for number, page in enumerate(document.pages, start=1):
            for name in (getattr(page, "anchors", None) or {}):
                if str(name).startswith("sec-"):
                    found.setdefault(str(name), number)
        if not found:
            # A WeasyPrint that keeps no anchor table on its pages: read the
            # section ids off the laid-out boxes instead.
            for number, page in enumerate(document.pages, start=1):
                stack = [getattr(page, "_page_box", None)]
                while stack:
                    box = stack.pop()
                    if box is None:
                        continue
                    element = getattr(box, "element", None)
                    ident = element.get("id") if hasattr(element, "get") else None
                    if ident and str(ident).startswith("sec-"):
                        found.setdefault(str(ident), number)
                    stack.extend(getattr(box, "children", None) or [])
        log.info("Contents page numbers: %d section(s) placed on %d page(s)",
                 len(found), len(document.pages))
        if not found:
            log.warning("Contents page numbers: no section anchors found; the "
                        "Word contents will show placeholders")
        return found
    except Exception:                                   # noqa: BLE001
        log.warning("Could not work out the contents page numbers", exc_info=True)
        return {}


def render_pdf(html: str, base_url: str = None) -> bytes:
    """Render assembled HTML into PDF bytes. Raises if WeasyPrint is absent."""
    import weasyprint
    return weasyprint.HTML(string=html, base_url=base_url).write_pdf()


# --------------------------------------------------------------------------
# Working marks stay out of the delivered document
# --------------------------------------------------------------------------
_CLEAN_RULES = [
    # a figure nobody could source: an empty cell, never the word
    (re.compile(r">\s*Incomplete\s*<"), "><"),
    (re.compile(r'<p class="held-table">.*?</p>', re.S), ""),
    (re.compile(r'<span class="missing-binding"[^>]*>.*?</span>', re.S), ""),
    (re.compile(r'<tr class="working-note">.*?</tr>', re.S), ""),
    # last year's wording awaiting this year's figure, and stray placeholders
    (re.compile(r"\s?\[update:[^\]]*\]"), lambda m: " S$______" if "S$" in m.group(0) else ""),
    (re.compile(r"\s?\[[^\]]*\bnot (?:provided|set)\]"), ""),
]


def clean_for_client(html):
    """The document without the app's own commentary.

    Everything the app says about the work - "Incomplete", "[update: ...]",
    "[... not provided]", "this draft cannot explain ..." - belongs to the
    preparer's review, where the same items are listed against their note. None
    of it is part of the accounts, so none of it is written into an export.
    """
    for pattern, replacement in _CLEAN_RULES:
        html = pattern.sub(replacement, html)
    return html
