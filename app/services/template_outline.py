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


def apply_to_report(report, template_path):
    """Switch a report's fixed sections to match the customer's template.

    Returns a short description of what changed, or None when nothing was
    decided (no template, unreadable contents page). Only ever turns
    sections OFF that the template lacks, and ON that it lists - a section
    the template lists is one the customer expects to see.
    """
    outline = read_outline(template_path)
    if outline is None:
        return None

    switched_off, switched_on = [], []
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

    if not (switched_off or switched_on):
        return None

    parts = []
    if switched_off:
        parts.append("left out " + ", ".join(switched_off))
    if switched_on:
        parts.append("included " + ", ".join(switched_on))
    return "; ".join(parts)
