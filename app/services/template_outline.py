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
    return titles


# Only the statements take their title from the template. The directors'
# statement is left as it is: whether it is "Director's" or "Directors'" is a
# fact about the client, not the firm the template came from.
TITLED_SECTIONS = {"statement_comprehensive_income",
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
    }
    for section in report.sections:
        rows = statement_profile.get(section.section_key)
        if rows and section.section_key in outline:
            binding = dict(section.data_binding or {})
            binding["presentation"] = rows
            section.data_binding = binding
            lined += 1

    aligned = _align_notes(report, profile)

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
                })
                binding["labels"] = labels
                section.data_binding = binding
                covered = True

    if not (switched_off or switched_on or retitled or covered
            or lined or aligned):
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
    return "; ".join(parts)
