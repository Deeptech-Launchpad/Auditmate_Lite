"""Last year's notes with their paragraphs and headings, read from the PDF.

The wording of the signed accounts' notes is first read through an AI service,
which does not always keep the line breaks: the same note can come back with
its paragraphs, or as one block with "3.1 Critical judgements ..." buried
inside the sentences. Everything downstream (paragraphs, headings, the
"[update: ...]" markers) depends on those breaks.

The PDF has the structure itself. A heading is set in a different font from
the body, and the space between lines is about ten points inside a paragraph
and nineteen or more between paragraphs. This reads the notes back from that,
and is used only where the stored wording has come through flat.
"""
import logging
import re
from collections import Counter
from pathlib import Path

log = logging.getLogger(__name__)

_NOTE_HEAD = re.compile(r"^(\d+)\.\s+\S")
_CONTINUED = re.compile(r"\(continued\)\s*$", re.IGNORECASE)
_PAGE_HEAD = re.compile(
    r"^(NOTES TO THE FINANCIAL STATEMENTS|FOR THE FINANCIAL YEAR ENDED)", re.IGNORECASE)
_BULLET = re.compile(r"^[•●\-]\s")
_LIST_START = re.compile(r"issued but not yet effective:\s*$", re.IGNORECASE)
_LIST_END = re.compile(r"^consequential amendments", re.IGNORECASE)

# Space between two lines of one paragraph is about 10; between paragraphs 19+.
PARAGRAPH_GAP = 15


def is_flat(text):
    """Whether stored wording has lost its paragraph breaks."""
    text = text or ""
    return len(text) > 600 and text.count("\n\n") < 1 + len(text) // 2500


def _page_lines(page):
    words = page.extract_words(extra_attrs=["fontname"], keep_blank_chars=False)
    rows = {}
    for word in words:
        rows.setdefault(round(word["top"] / 2), []).append(word)
    lines = []
    for key in sorted(rows):
        ws = sorted(rows[key], key=lambda w: w["x0"])
        font = Counter(w["fontname"] for w in ws).most_common(1)[0][0]
        lines.append({"text": " ".join(w["text"] for w in ws), "font": font,
                      "top": min(w["top"] for w in ws)})
    return lines


def read_notes(path):
    """{note number: text} - paragraphs separated by a blank line, each heading
    on a line of its own. None if the file cannot be read."""
    import pdfplumber

    path = Path(str(path))
    if path.suffix.lower() != ".pdf" or not path.exists():
        return None
    try:
        with pdfplumber.open(str(path)) as pdf:
            pages = [_page_lines(p) for p in pdf.pages[:40]]
    except Exception:                                        # noqa: BLE001
        log.exception("Could not read the structure of %s", path)
        return None

    fonts = Counter()
    for lines in pages:
        for line in lines:
            fonts[line["font"]] += len(line["text"].split())
    if not fonts:
        return None
    body = fonts.most_common(1)[0][0]

    notes, number, blocks = {}, None, []
    skipping = False

    def close():
        if number and blocks:
            notes[number] = "\n\n".join(blocks)

    for lines in pages:
        previous_top, join_next = None, bool(blocks)
        for line in lines:
            text = line["text"].strip()
            if not text or _PAGE_HEAD.match(text) or re.fullmatch(r"\d{1,3}", text):
                continue
            if re.match(r"^BROWN ROCK|^[A-Z .,&'()-]{6,}$", text) and line["font"] != body \
                    and previous_top is None and not _NOTE_HEAD.match(text):
                previous_top = line["top"]
                continue                                    # the page's company name
            heading = line["font"] != body and len(text) <= 120
            if heading and _CONTINUED.search(text):
                previous_top = line["top"]
                continue                        # the running head of a later page
            if heading and _NOTE_HEAD.match(text):
                close()
                number = _NOTE_HEAD.match(text).group(1)
                blocks, join_next = [], False
                previous_top = line["top"]
                continue
            if number is None:
                previous_top = line["top"]
                continue
            # A list the first reading lost, put back separately (see
            # template_tables): leave it out of the running text.
            if _LIST_START.search(text):
                skipping = True
            elif skipping and _LIST_END.match(text):
                skipping = False
            elif skipping:
                previous_top = line["top"]
                continue

            gap = None if previous_top is None else line["top"] - previous_top
            previous_top = line["top"]
            if heading:
                blocks.append(text)
                blocks.append("")                            # marks: heading done
                join_next = False
                continue
            new_paragraph = (gap is not None and gap > PARAGRAPH_GAP) or not blocks \
                or blocks[-1] == ""
            if join_next and gap is None and blocks and blocks[-1] != "":
                # first line of a new page continues the paragraph above it
                # unless that one already ended a sentence
                new_paragraph = bool(re.search(r"[.:;]$", blocks[-1])) and text[:1].isupper()
            join_next = False
            if _BULLET.match(text) and blocks and blocks[-1] != "":
                blocks[-1] += "\n" + text
            elif new_paragraph:
                if blocks and blocks[-1] == "":
                    blocks.pop()                    # heading marker consumed
                blocks.append(text)
            else:
                blocks[-1] += " " + text
    close()
    # drop heading markers
    return {n: re.sub(r"\n\n\n+", "\n\n", t.replace("\n\n\n", "\n\n")).strip()
            for n, t in ((n, "\n\n".join(b for b in t.split("\n\n") if b.strip()))
                         for n, t in notes.items())}


# ---- the directors' statement -------------------------------------------------
_HOLDING_ROW = re.compile(r"^(?P<name>.+?)\s+(?P<open>[\d,]+)\s+(?P<close>[\d,]+)$")


def read_directors_statement(path):
    """The template's directors' statement, structure kept.

    {"blocks": [(kind, indent_pt, text)], "holdings": [(name, opened, closed)]}
    where kind is "heading", "para" or "table" (where the shareholdings table
    sat). None if the file has no such statement.

    Headings are set in a different font; a clause "(a) ..." starts further in
    than the line above it and its continuation lines sit further in again, so
    a paragraph's indent is where its first line starts.
    """
    import pdfplumber

    path = Path(str(path))
    if path.suffix.lower() != ".pdf" or not path.exists():
        return None
    try:
        with pdfplumber.open(str(path)) as pdf:
            pages = []
            for page in pdf.pages[:12]:
                words = page.extract_words(extra_attrs=["fontname"])
                rows = {}
                for word in words:
                    rows.setdefault(round(word["top"] / 2), []).append(word)
                lines = []
                for key in sorted(rows):
                    ws = sorted(rows[key], key=lambda w: w["x0"])
                    lines.append({"text": " ".join(w["text"] for w in ws),
                                  "x0": ws[0]["x0"], "top": ws[0]["top"],
                                  "font": Counter(w["fontname"] for w in ws)
                                  .most_common(1)[0][0]})
                pages.append(lines)
    except Exception:                                        # noqa: BLE001
        log.exception("Could not read the directors' statement of %s", path)
        return None

    fonts = Counter()
    for lines in pages:
        for line in lines:
            fonts[line["font"]] += len(line["text"].split())
    if not fonts:
        return None
    body = fonts.most_common(1)[0][0]

    # The statement's pages: headed "DIRECTOR'S STATEMENT" on the second line
    # of the running head, up to the first statement page.
    statement_pages = []
    for lines in pages:
        head = " ".join(l["text"] for l in lines[:4]).upper()
        if "UNAUDITED STATEMENT OF" in head or "STATEMENT OF PROFIT" in head:
            break
        second = lines[1] if len(lines) > 1 else None
        if (second and second["top"] < 70
                and re.fullmatch(r"DIRECTOR.?S STATEMENT",
                                 second["text"].strip().upper())):
            statement_pages.append(lines)
    if not statement_pages:
        return None

    left = min(l["x0"] for lines in statement_pages for l in lines)
    blocks, holdings = [], []
    in_table = False
    previous_top = None
    for lines in statement_pages:
        previous_top = None
        for line in lines:
            text = line["text"].strip()
            if line["top"] < 66 or not text or re.fullmatch(r"\d{1,3}", text):
                continue                                    # running head, folio
            heading = line["font"] != body and len(text) < 90
            indent = max(0.0, line["x0"] - left)
            gap = None if previous_top is None else line["top"] - previous_top
            previous_top = line["top"]

            if re.match(r"^number of ordinary shares", text, re.IGNORECASE):
                in_table = True
                blocks.append(("table", 0, ""))
                continue
            if in_table:
                if heading and re.match(r"^\d+\.", text):
                    in_table = False
                else:
                    match = _HOLDING_ROW.match(text)
                    if match and not re.match(r"^(name|at the)", text, re.IGNORECASE):
                        holdings.append((match.group("name").strip(),
                                         match.group("open"), match.group("close")))
                    continue
            if heading:
                blocks.append(("heading", 0, text))
                continue
            new = (not blocks or blocks[-1][0] != "para" or gap is None
                   or gap > 15 or (indent > 20 and blocks[-1][1] < indent - 10
                                   and gap > 12))
            if new:
                blocks.append(("para", indent if indent < 40 else indent, text))
            else:
                kind, first_indent, previous = blocks[-1]
                blocks[-1] = (kind, first_indent, previous + " " + text)
    return {"blocks": blocks, "holdings": holdings}
