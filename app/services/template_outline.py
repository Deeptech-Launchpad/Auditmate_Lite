"""Which statements a customer's own report contains.

A customer who brings their own report brings its shape too: Brown Rock's
signed accounts have no separate detailed profit and loss, and an
Auditmate report that prints one anyway is not "their report". The contents
page of the template says which parts it has, so it is read here and the
fixed sections of a new report are switched to match.

Only the fixed sections are decided this way. Notes come from the notes
library and the trial balance, and are left alone.

Conservative on purpose. A template whose contents page cannot be found or
understood returns None, and the caller keeps the standard sections - a
wrong guess would switch off a statement the customer needs, which is worse
than showing one they did not ask for.
"""
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# section_key -> what its title looks like once punctuation is stripped.
# Punctuation is stripped first because a PDF's apostrophe comes out as
# anything at all ("Director�s statement").
SECTION_PATTERNS = {
    "directors_statement": r"directors? s? ?statement|statement by (the )?directors?",
    "independent_auditors_report": r"independent auditors?",
    "statement_comprehensive_income":
        r"profit or loss|comprehensive income|income statement",
    "statement_financial_position": r"financial position|balance sheet",
    "statement_changes_equity": r"changes in equity",
    "statement_cash_flows": r"cash flows?",
    "detailed_profit_and_loss": r"detailed (profit|income|statement)",
}

# Sections a template is never asked about - always kept.
ALWAYS_KEPT = {"cover_page"}

# Below this many recognised statements the contents page is not trusted:
# it may have been a different kind of document altogether.
MIN_RECOGNISED = 2

_CONTENTS_HEADING = re.compile(r"table of contents|contents", re.IGNORECASE)


def _normalise(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def _contents_text_pdf(path):
    import pdfplumber

    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages[:6]:
            text = page.extract_text() or ""
            head = "\n".join(text.splitlines()[:6])
            if _CONTENTS_HEADING.search(head):
                return text
    return None


def _contents_text_docx(path):
    from docx import Document

    lines = [p.text for p in Document(str(path)).paragraphs if p.text.strip()]
    for index, line in enumerate(lines[:40]):
        if _CONTENTS_HEADING.search(line):
            return "\n".join(lines[index:index + 40])
    return None


def read_outline(template_path):
    """{section_key: bool} for the template's fixed sections, or None.

    True where the template's contents page lists that section, False where
    it does not. None when there is no template, it is not readable, or its
    contents page could not be found.
    """
    if not template_path or template_path == "STANDARD":
        return None
    path = Path(template_path)
    if not path.exists():
        return None

    try:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            text = _contents_text_pdf(path)
        elif suffix == ".docx":
            text = _contents_text_docx(path)
        else:
            return None
    except Exception:                                      # noqa: BLE001
        log.exception("Could not read the contents of template %s", path)
        return None

    if not text:
        return None

    normalised = _normalise(text)
    found = {key: bool(re.search(pattern, normalised))
             for key, pattern in SECTION_PATTERNS.items()}

    if sum(found.values()) < MIN_RECOGNISED:
        return None
    return found


_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")

_PAGE_TAIL = re.compile(r"\s+\d{1,3}(?:\s*[-–]\s*\d{1,3})?\s*$")
_DATE_IN_LINE = re.compile(
    r"(\d{1,2})\s*([A-Za-z]{3,9})\s*(\d{4})")
_REG_LINE = re.compile(r"\(?\s*(registration|company|reg)\.?\s*(no|number)",
                      re.IGNORECASE)
_COMPANY_LINE = re.compile(
    r"(?<!\w)(pte|ltd|limited|llp|llc|inc|berhad|sdn)(?!\w)", re.IGNORECASE)


def _tidy(text):
    """A PDF's apostrophe often comes out as a replacement character."""
    return " ".join(text.replace("�", "'").split())


def _first_page_lines_pdf(path):
    import pdfplumber

    with pdfplumber.open(str(path)) as pdf:
        if not pdf.pages:
            return []
        text = pdf.pages[0].extract_text() or ""
    return [_tidy(line) for line in text.splitlines() if line.strip()]


def _first_page_lines_docx(path):
    from docx import Document

    lines = []
    for paragraph in Document(str(path)).paragraphs:
        text = _tidy(paragraph.text)
        if not text:
            continue
        if _CONTENTS_HEADING.search(text):
            break
        lines.append(text)
        if len(lines) >= 8:
            break
    return lines


def read_cover(path):
    """The wording of a template's title page, or None.

    A title page is a company name, a registration line, a document title of
    a line or two, and the period it covers. Each is picked out by its shape,
    and the company name and registration number are NOT taken - they belong
    to the customer being reported on, not the template's owner. What is
    kept is the document title and how the period is introduced ("FINANCIAL
    YEAR ENDED").

    None unless both a title and a period line are found: a page that does
    not look like a title page is left alone.
    """
    path = Path(path)
    try:
        lines = (_first_page_lines_pdf(path) if path.suffix.lower() == ".pdf"
                 else _first_page_lines_docx(path)
                 if path.suffix.lower() == ".docx" else [])
    except Exception:                                      # noqa: BLE001
        log.exception("Could not read the title page of %s", path)
        return None

    title, prefix, registration, upper_date = [], None, None, False
    for line in lines[:10]:
        if _REG_LINE.search(line):
            registration = re.sub(r"[():].*$", "", line.strip("() ")).strip()
            registration = re.split(r"[:\d]", registration)[0].strip() or None
            continue
        date = _DATE_IN_LINE.search(line)
        if date and date.group(2)[:3].lower() in _MONTHS:
            prefix = line[:date.start()].strip(" :,-") or None
            upper_date = line.isupper()
            continue
        if _COMPANY_LINE.search(line) and not title:
            continue                    # the template owner's own name
        title.append(line)

    if not title or prefix is None:
        return None
    return {"title": " ".join(title[:3]), "date_prefix": prefix,
            "registration_label": registration, "upper_date": upper_date}


def read_titles(template_path):
    """{section_key: title as the template's contents page words it}."""
    if not template_path or template_path == "STANDARD":
        return {}
    path = Path(template_path)
    if not path.exists():
        return {}
    try:
        text = (_contents_text_pdf(path) if path.suffix.lower() == ".pdf"
                else _contents_text_docx(path)
                if path.suffix.lower() == ".docx" else None)
    except Exception:                                      # noqa: BLE001
        return {}
    if not text:
        return {}

    titles = {}
    for raw in text.splitlines():
        tidy = _tidy(raw)
        # A contents ENTRY ends in the page it is on. The page's own heading
        # ("... UNAUDITED FINANCIAL STATEMENTS") has none, and matched the
        # directors' statement as a title far longer than the entry.
        if not _PAGE_TAIL.search(tidy):
            continue
        line = _PAGE_TAIL.sub("", tidy).strip()
        if not line or _CONTENTS_HEADING.search(line):
            continue
        normalised = _normalise(line)
        for key, pattern in SECTION_PATTERNS.items():
            if key in titles or not re.search(pattern, normalised):
                continue
            # Sentence case, as the contents page prints it: an all-capitals
            # line would otherwise be stamped over every statement heading.
            titles[key] = line[:1].upper() + (line[1:].lower()
                                              if line.isupper() else line[1:])
        if "notes_heading" not in titles and re.match(
                r"^notes to the financial statements?$", line, re.IGNORECASE):
            titles["notes_heading"] = line[:1].upper() + (
                line[1:].lower() if line.isupper() else line[1:])
    return titles


# The statements and the directors' statement take their title from the
# template: it is the customer's own report, so "Director's statement" stays
# singular where the company has one director.
TITLED_SECTIONS = {"directors_statement", "statement_comprehensive_income",
                   "statement_financial_position",
                   "statement_changes_equity", "statement_cash_flows"}


def _align_notes(report, profile):
    """Point three notes at the same lines as the statements drawn above them.

    The statements are drawn in the customer's lines: Administrative expenses
    without the income and finance items, Other income with the interest, and
    one "Trade and other receivables". The notes behind them were built from
    the standard lines, so each disagreed with its statement by exactly the
    part that moved (note 6 by 20,768, note 10 by 11,563). They are set to
    list what the statement line adds up. Returns how many were changed.
    """
    from .reports import _operating_expense_keys

    pl = {row["type"] for row in profile.get("profit_and_loss") or []}
    bs = {row["type"]: row for row in profile.get("balance_sheet") or []}
    income = {"other_income", "interest_income", "iras_rebate"}
    finance = {"interest_expense"}

    def rekey(section, keys):
        specs = [dict(spec) for spec in
                 (section.data_binding or {}).get("note_table_specs") or []]
        if not specs:
            return False
        specs[0]["keys"] = keys
        # The caption over these tables was the wording of one library piece
        # ("Carrying amount of transferred assets | Associated liabilities"
        # over the receivables), which describes a different disclosure.
        specs[0]["heading"] = None
        binding = dict(section.data_binding or {})
        binding["note_table_specs"] = specs
        section.data_binding = binding
        return True

    changed = 0
    for section in report.sections:
        key = section.section_key
        if key == "note__administrative_and_other_expenses" and pl:
            keys = [k for k in _operating_expense_keys()
                    if not ("other_income" in pl and k in income)
                    and not ("finance_cost" in pl and k in finance)]
            changed += rekey(section, keys)
        elif key == "note__other_income" and "other_income" in pl:
            changed += rekey(section, ["other_income", "interest_income",
                                       "iras_rebate"])
        elif key == "note__trade_receivables" and "receivables" in bs:
            if rekey(section, ["trade_receivables", "prepayments",
                               "contract_assets"]):
                section.title = bs["receivables"]["label"]
                changed += 1
    return changed


def apply_to_report(report, template_path):
    """Shape a new report like the customer's own template.

    Returns a short description of what changed, or None when nothing was
    decided (no template, unreadable contents page). Sections the template
    lacks are switched OFF and ones it lists are switched ON; the statements
    take the template's own titles; and the cover takes its title wording.
    """
    outline = read_outline(template_path)
    if outline is None:
        return None

    titles = read_titles(template_path)
    cover = read_cover(template_path)

    switched_off, switched_on, retitled = [], [], 0
    for section in report.sections:
        key = section.section_key
        if key in ALWAYS_KEPT or key not in outline:
            continue
        if key == "directors_statement" and any(
                x.section_key == "note__S01_DIRECTORS_STATEMENT"
                and x.is_enabled for x in report.sections):
            continue        # the library's replaces it; see _arrange_statutory
        wanted = outline[key]
        if section.is_enabled and not wanted:
            section.is_enabled = False
            switched_off.append(section.title)
        elif not section.is_enabled and wanted:
            section.is_enabled = True
            switched_on.append(section.title)
        if key in TITLED_SECTIONS and titles.get(key)                 and titles[key] != section.title:
            section.title = titles[key]
            retitled += 1

    lined = 0
    from . import template_statements
    profile = template_statements.read_profile(template_path)
    statement_profile = {
        "statement_comprehensive_income": profile.get("profit_and_loss"),
        "statement_financial_position": profile.get("balance_sheet"),
        "statement_changes_equity": profile.get("changes_in_equity"),
    }
    for section in report.sections:
        if section.section_key == "statement_cash_flows" and profile.get("cash_flow_wording"):
            binding = dict(section.data_binding or {})
            wording = dict(profile["cash_flow_wording"])
            try:
                from . import template_note_tables
                table = template_note_tables.read_statement(
                    template_path, r"statement of cash flows")
            except Exception:                              # noqa: BLE001
                log.exception("Could not read the template's cash flow rows")
                table = None
            if table:
                wording["rows"] = [
                    {"label": r["label"], "kind": r["kind"],
                     "cells": [str(c) for c in r["cells"]] if r["cells"] else None}
                    for r in table["rows"]]
            binding["cash_flow_wording"] = wording
            section.data_binding = binding
        rows = statement_profile.get(section.section_key)
        if rows and section.section_key in outline:
            binding = dict(section.data_binding or {})
            binding["presentation"] = rows
            if section.section_key == "statement_financial_position":
                binding["headings"] = profile.get("balance_sheet_headings") or {}
            section.data_binding = binding
            lined += 1

    aligned = _align_notes(report, profile)
    directors = _directors_statement(report, template_path)
    if directors:
        aligned = (aligned or 0) + 1

    covered = False
    if cover:
        for section in report.sections:
            if section.section_key == "cover_page":
                binding = dict(section.data_binding or {})
                labels = dict(binding.get("labels") or {})
                labels.update({
                    "template_cover": True,
                    "title": cover["title"],
                    "date_prefix": cover["date_prefix"],
                    "registration_label": (cover["registration_label"]
                                           or "Registration No"),
                    "upper_date": cover["upper_date"],
                    "notes_title": titles.get("notes_heading"),
                })
                binding["labels"] = labels
                section.data_binding = binding
                covered = True

    from . import template_note_tables, template_skeleton
    followed = template_skeleton.apply(report)
    if followed:
        tabled = template_note_tables.apply(report, template_path)
        if tabled:
            followed += f"; drew the tables of {tabled} notes as it does"

    if not (switched_off or switched_on or retitled or covered
            or lined or aligned or followed):
        return None

    parts = []
    if switched_off:
        parts.append("left out " + ", ".join(switched_off))
    if switched_on:
        parts.append("included " + ", ".join(switched_on))
    if retitled:
        parts.append(f"used its wording for {retitled} statement title(s)")
    if covered:
        parts.append("used its cover page wording")
    if lined:
        parts.append(f"drew {lined} statement(s) in its own lines")
    if aligned:
        parts.append(f"matched {aligned} note(s) to those lines")
    if followed:
        parts.append(followed)
    return "; ".join(parts)


_DATE_LINE = re.compile(r"^\d{1,2}\s+[A-Za-z]+\s+\d{4}$")


def _shareholdings_html(customer_id, financial_year, template_holdings):
    """The directors' shareholdings table, in the template's columns.

    This year's figures where the share register (or the signed set's own
    statement, read into the registers) gives them; otherwise the template's
    closing holdings carried across - shares are unchanged where the share
    capital is, which the share capital note already reasons from.
    """
    from html import escape

    from . import bindings

    rows = []
    try:
        for name, opened, closed in bindings.shareholdings(financial_year):
            rows.append((name, opened, closed))
    except Exception:                                        # noqa: BLE001
        rows = []
    if not rows:
        for name, _opened, closed in template_holdings or []:
            number = float(str(closed).replace(",", "") or 0)
            rows.append((name, number, number))
    if not rows:
        return ('<p class="held-table">Incomplete &mdash; the directors\' '
                "holdings at both dates need the share register.</p>")

    def figure(value):
        return "\u2014" if value in (None, "") else "{:,.0f}".format(value)

    body = "".join(
        '<tr><td class="lbl">%s</td><td class="num">%s</td><td class="num">%s</td></tr>'
        % (escape(name), figure(opened), figure(closed))
        for name, opened, closed in rows)
    return (
        '<table class="fin note-table shareholdings"><thead>'
        '<tr><th class="lbl"></th><th class="num" colspan="2">Number of ordinary shares</th></tr>'
        '<tr><th class="lbl"></th><th class="num" colspan="2">Direct interest</th></tr>'
        '<tr><th class="lbl">Name of directors in which interest is held</th>'
        '<th class="num">At the beginning of the year</th>'
        '<th class="num">At the end of the year</th></tr></thead><tbody>'
        '<tr><td class="lbl">The Company</td><td></td><td></td></tr>'
        + body + "</tbody></table>")


def directors_statement_html(template_path, financial_year, company_name):
    """The customer's own directors' statement, or None.

    The wording is the template's - its clauses, its order, its indents - with
    the year's dates rolled on, and the company, the director(s) and the date
    of signing taken from this engagement rather than typed. A note that says
    "the director" stays singular because the template's does; whoever
    finishes the document edits it, as with any carried wording.
    """
    from html import escape

    from . import reports, template_structure

    read = template_structure.read_directors_statement(template_path)
    if not read or not read["blocks"]:
        return None

    template_name = (company_name or "").strip()
    out, after_office_line = [], False
    for kind, indent, text in read["blocks"]:
        if kind == "table":
            out.append(_shareholdings_html(None, financial_year, read["holdings"]))
            continue
        text = reports.roll_forward_text(text, financial_year)
        if template_name and template_name.lower() in text.lower():
            text = re.sub(re.escape(template_name), "{{ customer.legal_name }}",
                          text, flags=re.IGNORECASE)
        if kind == "heading":
            if re.match(r"^signed by", text, re.IGNORECASE):
                out.append(f"<p>{escape(text)}</p>")
            else:
                # the signature's name line was set in the heading font
                out.append(f"<p><strong>{{{{ field.signing_directors }}}}</strong></p>"
                           if not re.match(r"^\d+\.", text) else
                           f"<h4>{escape(text)}</h4>")
            continue
        if re.search(r"in office at the date of this statement is:", text):
            out.append(f'<p class="ind1">{escape(text)}</p>')
            after_office_line = True
            continue
        if after_office_line:
            out.append('<p class="ind1">{{ field.director_names }}</p>')
            after_office_line = False
            continue
        if _DATE_LINE.match(text.strip()):
            out.append("<p>{{ field.report_date }}</p>")
            continue
        css = ' class="ind1"' if indent >= 20 else ""
        out.append(f"<p{css}>{text if '{{' in text else escape(text)}</p>")
    return "\n".join(out)


def _directors_statement(report, template_path):
    """Put the template's directors' statement in place of the generic one."""
    financial_year = report.financial_year
    cover = read_cover(template_path) or {}
    company = None
    try:
        lines = _first_page_lines_pdf(template_path) if str(
            template_path).lower().endswith(".pdf") else []
        company = lines[0] if lines else None
    except Exception:                                        # noqa: BLE001
        company = None
    html = directors_statement_html(template_path, financial_year, company)
    if not html:
        return False
    fixed = next((s for s in report.sections
                  if s.section_key == "directors_statement"), None)
    if fixed is None:
        return False
    fixed.content_html = html
    fixed.is_enabled = True
    title = read_titles(template_path).get("directors_statement")
    if title:
        fixed.title = title           # "Director's statement", as the template
    for section in report.sections:
        if section.section_key in ("note__S01_DIRECTORS_STATEMENT",
                                   "note__S03_STATEMENT_BY_DIRECTORS_SIG",
                                   "note__S02_COMPILATION_REPORT"):
            # the template's replaces the first and third; it has no
            # compilation report, so that one stays off (Sections can re-enable)
            section.is_enabled = False
    return True
