"""Tables of words in last year's accounts that the first reading lost.

The signed accounts' notes are read into plain text when the template is
uploaded, and a table with no ruled lines comes through as nothing. The
policies note announces "the following FRS and INT FRS ... were issued but not
yet effective:" and the list that follows - the standard, its title, its
effective date - was dropped, so the carried note ended on a colon.

This reads that table back from the template's own text and returns it as a
table, or None when it cannot be read cleanly (in which case nothing is
invented and the note says so).
"""
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_START = re.compile(r"issued but not yet effective:\s*$", re.IGNORECASE)
_END = re.compile(r"^consequential amendments", re.IGNORECASE)
_HEADER = re.compile(
    r"^(effective date|\(annual periods|beginning|frs title on or after\)?)",
    re.IGNORECASE)
_DATE = re.compile(r"\b(1 January \d{4}|To be determined)\b")
_LABEL = re.compile(r"^((?:FRS\s*\d+\s*,?\s*)+|Various)\s+(?P<rest>.+)$",
                    re.IGNORECASE)
_SECOND_PART = re.compile(r"^(FRS\s*\d+)(?:\s+(?P<rest>.+))?$", re.IGNORECASE)


def _lines(path):
    import pdfplumber

    out = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages[:40]:
            out.extend(ln.strip() for ln in (page.extract_text() or "").splitlines())
    return out


def _dated(text):
    return bool(_DATE.search(text)) or ("To be" in text and "determined" in text)


def standards_rows(path):
    """[(standard, title, effective date)] for the "not yet effective" table."""
    try:
        lines = _lines(path)
    except Exception:                                        # noqa: BLE001
        log.exception("Could not read %s", path)
        return None

    first = next((i for i, ln in enumerate(lines) if _START.search(ln)), None)
    if first is None:
        return None
    end = next((i for i in range(first + 1, len(lines))
                if _END.match(lines[i])), None)
    if end is None:
        return None
    # The announcing sentence is the LAST one before the list: the note says
    # "issued but not yet effective:" twice, a heading and then the sentence.
    begin = max(i for i in range(first, end) if _START.search(lines[i]))

    body = [ln for ln in lines[begin + 1:end]
            if ln and not _HEADER.match(ln) and not re.match(
                r"^(NOTES TO|FOR THE FINANCIAL|\d+$)", ln)]

    # A row starts at a line beginning "FRS n" (or "Various") once the row
    # before it is complete. A row whose standard number runs on to the next
    # line ("FRS 7," / "FRS 117 Arrangements") is not complete until it has it.
    rows, current = [], []
    for line in body:
        starts = bool(re.match(r"^(FRS\s*\d+|Various)\b", line, re.IGNORECASE))
        split_label = bool(current and len(current) == 1
                           and re.match(r"^FRS\s*\d+\s*,", current[0], re.IGNORECASE))
        if starts and current and _dated(" ".join(current)) and not split_label:
            rows.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        rows.append(current)

    parsed = []
    for row in rows:
        row = list(row)
        match = _LABEL.match(row[0])
        if not match:
            return None
        standard = " ".join(match.group(1).split())
        row[0] = match.group("rest")
        if standard.endswith(","):
            # The standard's second part starts a following line.
            for index in range(1, len(row)):
                second = _SECOND_PART.match(row[index])
                if second:
                    standard = f"{standard} {second.group(1)}"
                    row[index] = second.group("rest") or ""
                    break
        text = " ".join(row)
        if "To be" in text and "determined" in text:
            date = "To be determined"
            text = text.replace("To be", "", 1).replace("determined", "", 1)
        else:
            found = _DATE.search(text)
            if not found:
                return None
            date = found.group(1)
            text = text.replace(found.group(1), "", 1)
        parsed.append((standard.rstrip(","), " ".join(text.split()), date))
    return parsed or None


def standards_table_html(path):
    """The table as HTML, or None."""
    from html import escape

    rows = standards_rows(path)
    if not rows:
        return None
    body = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
        % tuple(escape(cell) for cell in row) for row in rows)
    return ('<table class="fin note-table grading"><thead><tr><th>FRS</th>'
            "<th>Title</th><th>Effective date (annual periods beginning on "
            f"or after)</th></tr></thead><tbody>{body}</tbody></table>")


def restore_standards_table(html, template_path):
    """Put the table back where the carried note announces it.

    Only where the note has the announcing sentence and the very next thing is
    "Consequential amendments": the list was lost between them. Leaves any
    note that already has a table there alone.
    """
    if not html or "not yet effective" not in html:
        return html
    marker = re.search(r"<p>\s*Consequential amendments were also made", html)
    if not marker or "<table" in html[max(0, marker.start() - 400):marker.start()]:
        return html
    if not template_path or not Path(str(template_path)).exists():
        return html
    table = standards_table_html(template_path)
    if not table:
        return html
    return html[:marker.start()] + table + "\n" + html[marker.start():]
