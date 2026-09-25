"""The unaudited financial statements as an editable Word document.

The firm's deliverable: *"A set of unaudited financial statements as an
editable Word document... That document is the deliverable. The app's job
ends when it is produced."*

Editable is the whole point. A PDF is finished; a Word file is where the
preparer changes a figure, rewrites a note, or adds one that was never in
the list. So this does not try to be a perfect rendering - it tries to be a
good starting draft that a person then finishes.

It converts the very HTML the preview already renders, rather than walking
the report model a second time. One rendering path means the Word file and
the on-screen preview cannot drift apart, and every improvement to the
statements shows up in both. The alternative - a separate docx builder
reading the same data - is two things to keep in step, and they never stay
in step.

What survives the conversion: headings, paragraphs, lists, tables with
their header rows, and bold and italic runs. What does not: colour, page
backgrounds, and CSS layout, none of which belong in a document somebody is
about to edit anyway.
"""
import io
import logging
import re
from html.parser import HTMLParser

from docx import Document as DocxDocument
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Mm, Pt, RGBColor

log = logging.getLogger(__name__)

HEADINGS = {"h1": 0, "h2": 1, "h3": 2, "h4": 3, "h5": 4, "h6": 4}
SKIP = {"script", "style", "head", "nav", "button", "form", "select"}
BLOCKS = {"p", "div", "li", "tr", "section", "article", "header", "footer"}

# A figure column should be right-aligned and a label column should not.
# Deciding by content is the only signal available once the CSS is gone.
NUMERIC = re.compile(r"^[\s(]*-?[\d,]+\.?\d*[\s)%]*$")

# What a <br> becomes between the reader and the writer. It has to survive
# _flush, which otherwise discards anything that is only whitespace.
LINE_BREAK = "\n"

# Column widths, in millimetres on A4 with 20 mm margins (170 mm usable).
# Fixed rather than left to Word's autofit, which sized every table to its
# own contents: the income statement, the balance sheet and the cash flow
# each came out with amount columns of a different width, so nothing lined
# up when a reader turned the page.
AMOUNT_COLUMN_MM = 26
MIN_LABEL_MM = 60


class _Reader(HTMLParser):
    """Turn the report's HTML into a flat list of instructions.

    Deliberately not a general HTML renderer. It handles what the report
    templates actually emit and ignores the rest, because a converter that
    tries to handle everything handles nothing predictably.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._skip_depth = 0
        self._text = []
        self._bold = 0
        self._italic = 0
        self._heading = None
        self._in_table = False
        self._row = None
        self._cell = None
        self._header_row = False
        self._cell_numeric = False
        # Where a cell's text should sit, and which sections it points at.
        # The contents page cites a page number through WeasyPrint's
        # target-counter, which is CSS and leaves the cell empty here - so
        # the anchors are carried through and become Word page references.
        self._cell_align = None
        self._cell_refs = []
        # A set of accounts is ruled, not gridded: a line under the column
        # headings, one above each subtotal, a double one under the final
        # total, and nothing anywhere else. Which row is which is on the
        # <tr> in the preview already, so it is read here rather than
        # guessed from the text later.
        self._row_kind = ""
        # Which open <div>s centre their text (the cover), so a paragraph
        # knows to be centred without the converter reading CSS.
        self._div_stack = []
        self._div_looks = []
        self._rule_head = False
        self._upper = 0
        self._centred = 0

    def _format_next(self, **what):
        self.out.append(("next", what, None))

    # -- text collection --------------------------------------------------

    def _flush(self):
        # A <br> arrives as a LINE_BREAK run. Filtering on strip() alone drops
        # it, which silently joined two directors' names into one on the first
        # page of the accounts - so keep it, and drop only real whitespace.
        runs = list(self._text)
        self._text = []
        # spaces at either end are indentation; one between two runs is a word gap
        while runs and not runs[0][0].strip() and runs[0][0] != LINE_BREAK:
            runs.pop(0)
        while runs and not runs[-1][0].strip() and runs[-1][0] != LINE_BREAK:
            runs.pop()

        # A break at either end of a block is spacing, not content.
        while runs and runs[0][0] == LINE_BREAK:
            runs.pop(0)
        while runs and runs[-1][0] == LINE_BREAK:
            runs.pop()
        # The run after a break starts a new line, so its leading space is
        # left over from the HTML's indentation rather than being content.
        for index in range(1, len(runs)):
            if runs[index - 1][0] == LINE_BREAK:
                runs[index] = (runs[index][0].lstrip(),
                               runs[index][1], runs[index][2])

        if runs:
            runs[0] = (runs[0][0].lstrip(), runs[0][1], runs[0][2])
            runs[-1] = (runs[-1][0].rstrip(), runs[-1][1], runs[-1][2])
        return [r for r in runs if r[0]]

    def _emit_block(self):
        runs = self._flush()
        if not runs:
            return
        if self._centred:
            self._format_next(align="center")
        if self._heading is not None:
            self.out.append(("heading", self._heading, runs))
        else:
            self.out.append(("para", None, runs))

    def handle_data(self, data):
        if self._skip_depth:
            return
        if not data.strip():
            # Keep a single space so "<b>Total</b> revenue" does not become
            # "Totalrevenue".
            if self._text and not self._text[-1][0].endswith(" "):
                self._text.append((" ", False, False))
            return
        text = re.sub(r"\s+", " ", data)
        if self._upper:
            text = text.upper()
        self._text.append((text, bool(self._bold), bool(self._italic)))

    # -- structure --------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        if tag in ("b", "strong"):
            self._bold += 1
        elif tag in ("i", "em"):
            self._italic += 1
        elif tag == "a":
            # The contents page links each entry to the section it names.
            # Kept so the page number can be a real Word field rather than
            # a number frozen at the moment the file was written.
            href = next((v for k, v in attrs if k == "href"), "") or ""
            if href.startswith("#sec-") and self._cell is not None:
                self._cell_refs.append(href[1:])
        elif tag == "div" and not self._in_table:
            # Still a block, as it was before: closing the open one is what
            # the BLOCKS branch below would have done. The anchor is extra.
            self._emit_block()
            classes = (next((v for k, v in attrs if k == "class"), "")
                       or "").split()
            anchor = next((v for k, v in attrs if k == "id"), "") or ""
            if anchor.startswith("sec-"):
                self.out.append(("bookmark", anchor, None))
            # A section that starts a page does so in the Word file too - the
            # converter used to run every section into the last.
            if "page-break" in classes:
                self._format_next(page_break=True)
            # what the PDF's stylesheet does to these blocks, which the Word
            # file cannot read: the statement title in bold capitals, the period
            # line in italic capitals, the contents heading in bold capitals
            look = ((1, 0, 1) if ("rpt-sect" in classes or "contents-title" in classes
                                  or "rpt-co" in classes)
                    else (0, 1, 1) if "rpt-period" in classes else (0, 0, 0))
            self._bold += look[0]
            self._italic += look[1]
            self._upper += look[2]
            self._div_looks.append(look)
            if "with-rule" in classes:
                self._rule_head = True
            if "rpt-period" in classes and self._rule_head:
                self._rule_head = False
                self._format_next(rule_under=True)
            centred = "cover" in classes
            self._div_stack.append(centred)
            if centred:
                self._centred += 1
                self._format_next(before=150)         # the upper third
        elif tag == "br":
            self._text.append((LINE_BREAK, False, False))
        elif tag in HEADINGS:
            self._emit_block()
            self._heading = HEADINGS[tag]
        elif tag == "table":
            self._emit_block()
            self._in_table = True
            classes = (next((v for k, v in attrs if k == "class"), "")
                       or "").split()
            self.out.append(("table_start", classes, None))
        elif tag == "tr" and self._in_table:
            self._row = []
            classes = (next((v for k, v in attrs if k == "class"), "")
                       or "").split()
            self._row_kind = ("total" if "total" in classes
                              else "subtotal" if "subtotal" in classes
                              else "group" if "group-head" in classes
                              else "")
        elif tag in ("td", "th") and self._in_table:
            self._cell = []
            self._text = []
            # The report marks every numeric column `class="num"` - the
            # figures themselves (see amount_cell) AND their header cells
            # (a date, a currency symbol). Read here because it is the one
            # reliable signal: guessing from a cell's own text later missed
            # a nil value ("--", no digit in it at all) and every header
            # cell (a date has two dots, "S$" has no digits either), which
            # left them left-aligned under a column of right-aligned figures.
            class_attr = next((v for k, v in attrs if k == "class"), "") or ""
            classes = class_attr.split()
            self._cell_numeric = "num" in classes
            self._cell_refs = []
            # A note reference is centred under its heading, a figure is
            # right-aligned, a page number on the contents page follows its
            # dot leader to the right margin.
            self._cell_align = ("center" if "notes" in classes
                                else "right" if self._cell_numeric
                                or "c-page" in classes else None)
            if tag == "th":
                self._header_row = True
        elif tag == "hr":
            self._emit_block()
            self.out.append(("rule", None, None))
        elif tag in BLOCKS and not self._in_table:
            self._emit_block()

    def handle_endtag(self, tag):
        if tag in SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return

        if tag in ("b", "strong"):
            self._bold = max(0, self._bold - 1)
        elif tag in ("i", "em"):
            self._italic = max(0, self._italic - 1)
        elif tag in HEADINGS:
            self._emit_block()
            self._heading = None
        elif tag == "div" and not self._in_table:
            self._emit_block()
            if self._div_looks:
                look = self._div_looks.pop()
                self._bold = max(0, self._bold - look[0])
                self._italic = max(0, self._italic - look[1])
                self._upper = max(0, self._upper - look[2])
            if self._div_stack and self._div_stack.pop():
                self._centred = max(0, self._centred - 1)
        elif tag in ("td", "th") and self._in_table:
            text = "".join(r[0] for r in self._flush()).strip()
            if self._row is not None:
                self._row.append((text, self._cell_align,
                                  tuple(self._cell_refs)))
            self._cell = None
            self._cell_numeric = False
            self._cell_align = None
            self._cell_refs = []
        elif tag == "tr" and self._in_table:
            if self._row:
                self.out.append(
                    ("row", (self._header_row, self._row_kind), self._row))
            self._row = None
            self._header_row = False
            self._row_kind = ""
        elif tag == "table":
            self.out.append(("table_end", None, None))
            self._in_table = False
        elif tag == "li":
            runs = self._flush()
            if runs:
                self.out.append(("bullet", None, runs))
        elif tag in BLOCKS and not self._in_table:
            self._emit_block()

    def close(self):
        super().close()
        self._emit_block()
        return self.out


def _element(tag, **attrs):
    """One w: element, built with the namespace python-docx expects."""
    node = OxmlElement(tag)
    for name, value in attrs.items():
        node.set(qn("w:" + name), str(value))
    return node


def _clear_table_borders(table):
    """Strip every rule from a table, so ruling can be added deliberately.

    Financial statements carry no vertical rules and no box around each
    cell. The converter was applying Word's "Table Grid", which draws all
    of them - so a balance sheet came out looking like a spreadsheet
    someone had printed, and a preparer's first act was to select the
    table and clear the borders by hand.
    """
    properties = table._tbl.tblPr
    for existing in properties.findall(qn("w:tblBorders")):
        properties.remove(existing)
    borders = _element("w:tblBorders")
    for edge in ("top", "left", "bottom", "right",
                 "insideH", "insideV"):
        borders.append(_element("w:" + edge, val="none", sz="0", space="0"))
    properties.append(borders)


def _rule_under(cell, double=False):
    """A single or double rule under one cell, as an accountant draws it."""
    properties = cell._tc.get_or_add_tcPr()
    for existing in properties.findall(qn("w:tcBorders")):
        properties.remove(existing)
    borders = _element("w:tcBorders")
    borders.append(_element("w:bottom",
                            val="double" if double else "single",
                            sz="6", space="0", color="000000"))
    properties.append(borders)


def _rule_over(cell):
    """A rule above a cell: what sits over a subtotal."""
    properties = cell._tc.get_or_add_tcPr()
    borders = properties.find(qn("w:tcBorders"))
    if borders is None:
        borders = _element("w:tcBorders")
        properties.append(borders)
    borders.append(_element("w:top", val="single", sz="6", space="0",
                            color="000000"))


def _repeat_as_header(row):
    """Repeat this row at the top of every page the table runs onto."""
    properties = row._tr.get_or_add_trPr()
    properties.append(_element("w:tblHeader", val="true"))


def _keep_row_whole(row):
    """Do not break this row across a page. A figure split from its
    label over a page turn is unreadable in a set of accounts."""
    properties = row._tr.get_or_add_trPr()
    properties.append(_element("w:cantSplit", val="true"))


def _page_numbers(section, own_paragraph=False, spec=False):
    """PAGE of NUMPAGES, centred in the footer.

    Added as real Word fields rather than typed text, so they stay right
    when the preparer edits the document - which is the whole premise of
    delivering it in Word.

    `own_paragraph` is for a customer's Word template: its footer already
    says something ("Confidential"), so the numbers go on a new line under
    it and the template's own font is left alone.
    """
    paragraph = (section.footer.add_paragraph() if own_paragraph
                 else section.footer.paragraphs[0])
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER

    def field(instruction):
        begin = _element("w:fldChar", fldCharType="begin")
        instr = OxmlElement("w:instrText")
        instr.set(qn("xml:space"), "preserve")
        instr.text = instruction
        end = _element("w:fldChar", fldCharType="end")
        run = paragraph.add_run()
        run._r.append(begin)
        run._r.append(instr)
        run._r.append(end)

    if spec:
        # AuditMate_Output_Format_Spec.pdf: bottom centre, Arial 9 pt bold,
        # the number alone.
        field("PAGE")
        for run in paragraph.runs:
            run.font.name = "Arial"
            run.font.size = Pt(9)
            run.font.bold = True
        return
    paragraph.add_run("Page ")
    field("PAGE")
    paragraph.add_run(" of ")
    field("NUMPAGES")
    if not own_paragraph:
        for run in paragraph.runs:
            run.font.name = "Times New Roman"
            run.font.size = Pt(9)


def _footer_has_page_field(section):
    return any("PAGE" in (node.text or "")
               for node in section.footer._element.iter(qn("w:instrText")))


def _bm(name):
    """A Word bookmark name: letters, digits and underscores only."""
    return re.sub(r"\W", "_", name)


def _bookmark(paragraph, name, number):
    """Mark this paragraph so a page reference can point at it."""
    start = _element("w:bookmarkStart", id=str(number), name=_bm(name))
    end = _element("w:bookmarkEnd", id=str(number))
    paragraph._p.insert(0, start)
    paragraph._p.append(end)


def _page_ref(paragraph, bookmark, shown="1"):
    """PAGEREF: the page a bookmark lands on, as a field Word keeps right.

    The contents page gets its numbers from WeasyPrint's target-counter,
    which is CSS - so in the PDF it is correct and in the Word file the
    cell came out empty. A number written in here instead would be right
    once and wrong as soon as the preparer added a paragraph, which is
    the one thing a Word deliverable is for.
    """
    begin = _element("w:fldChar", fldCharType="begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGEREF %s \\h " % _bm(bookmark)
    separate = _element("w:fldChar", fldCharType="separate")
    placeholder = OxmlElement("w:t")
    placeholder.text = str(shown or "1")
    end = _element("w:fldChar", fldCharType="end")
    run = paragraph.add_run()
    for node in (begin, instr, separate, placeholder, end):
        run._r.append(node)
    return run


def _update_fields_on_open(document):
    """Ask Word to refresh every field when the document is opened.

    Without it a PAGEREF shows whatever was written as its placeholder
    until somebody presses F9 - and the acceptance test for this document
    is opening it and exporting a PDF without touching anything.
    """
    settings = document.settings.element
    for existing in settings.findall(qn("w:updateFields")):
        settings.remove(existing)
    settings.append(_element("w:updateFields", val="true"))


def _write_runs(paragraph, runs):
    for text, bold, italic in runs:
        if text == LINE_BREAK:
            paragraph.add_run().add_break()
            continue
        run = paragraph.add_run(text)
        run.bold = bold
        run.italic = italic


_KNOWN_FACES = (("times", "Times New Roman"), ("arial", "Arial"),
                ("helvetica", "Arial"), ("calibri", "Calibri"),
                ("cambria", "Cambria"), ("georgia", "Georgia"),
                ("garamond", "Garamond"), ("verdana", "Verdana"),
                ("tahoma", "Tahoma"), ("courier", "Courier New"))


def _pdf_look(path):
    """The layout a customer's signed PDF is set in, read off the PDF itself.

    A PDF carries no styles - converting it to Word (pdf2docx) gave back a
    document whose Normal style was blank, so the "template" contributed
    nothing and the export looked like the standard one. What a PDF does
    carry is measurable: the page size, how far the text sits from each
    edge, the size the body is set in, and the font's name. Those are what
    make one firm's accounts look different from another's, so they are
    read here and applied to the export.

    Returns None when the PDF cannot be read, so the caller falls back to
    the standard layout rather than failing the export.
    """
    import collections
    import statistics

    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            pages = [pg for pg in pdf.pages[:12] if pg.chars]
            if not pages:
                return None
            width, height = pages[0].width, pages[0].height
            sizes, faces = collections.Counter(), collections.Counter()
            left, right, top, bottom = [], [], [], []
            for pg in pages:
                for ch in pg.chars:
                    sizes[round(ch["size"] * 2) / 2] += 1
                    faces[ch["fontname"].split("+")[-1]] += 1
                left.append(min(c["x0"] for c in pg.chars))
                right.append(pg.width - max(c["x1"] for c in pg.chars))
                top.append(min(c["top"] for c in pg.chars))
                bottom.append(pg.height - max(c["bottom"] for c in pg.chars))
    except Exception:                                      # noqa: BLE001
        log.exception("Could not read the layout of PDF template %s", path)
        return None

    def mm(points):
        return points * 25.4 / 72

    face = "Times New Roman"
    lowered = faces.most_common(1)[0][0].lower()
    for needle, name in _KNOWN_FACES:
        if needle in lowered:
            face = name
            break

    # Text measured from the edge is the margin, but a page's first line can
    # be a running header sitting closer in; the median across pages ignores
    # a stray title page. Clamped so a badly scanned file cannot produce a
    # page with no room to write on.
    def margin(values, low, high):
        return min(max(mm(statistics.median(values)), low), high)

    return {
        "page_mm": (mm(width), mm(height)),
        "left": margin(left, 15, 45), "right": margin(right, 15, 45),
        # The text of a template's first line is often a running header
        # sitting close to the edge (Brown Rock's: 15 mm). Ours carries a
        # DRAFT band there, which then printed over the first line of every
        # page, so the top margin is never less than 24 mm.
        "top": margin(top, 24, 35), "bottom": margin(bottom, 15, 35),
        "body_pt": min(max(sizes.most_common(1)[0][0], 8), 12),
        "face": face,
    }


def _docx_look(path):
    """The same measurements as _pdf_look, read from a Word template."""
    try:
        document = DocxDocument(str(path))
        section = document.sections[0]
        normal = document.styles["Normal"].font
        return {
            "page_mm": (Emu(section.page_width).mm, Emu(section.page_height).mm),
            "left": Emu(section.left_margin).mm,
            "right": Emu(section.right_margin).mm,
            "top": Emu(section.top_margin).mm,
            "bottom": Emu(section.bottom_margin).mm,
            "body_pt": normal.size.pt if normal.size else 11,
            "face": normal.name or "Times New Roman",
        }
    except Exception:                                      # noqa: BLE001
        log.exception("Could not read the layout of DOCX template %s", path)
        return None


_LOOK_CACHE = {}


def template_look(template_path):
    """Layout of a customer's template, or None for the standard one.

    The preview, the PDF and the Word file all read this one answer, so
    they cannot disagree about what the customer's report looks like.
    Cached on the file's modification time: measuring a 24-page PDF on
    every page view would make the preview noticeably slow.
    """
    from pathlib import Path

    if not template_path or template_path == "STANDARD":
        return None
    path = Path(template_path)
    if not path.exists():
        return None
    key = (str(path), path.stat().st_mtime)
    if key not in _LOOK_CACHE:
        suffix = path.suffix.lower()
        _LOOK_CACHE[key] = (_pdf_look(path) if suffix == ".pdf"
                            else _docx_look(path) if suffix == ".docx"
                            else None)
    return _LOOK_CACHE[key]


def _load_docx_template(template_path):
    """A customer's own Word file, emptied of its text but keeping the page
    setup, styles, headers and footers it is built on. None if unusable."""
    from pathlib import Path

    path = Path(template_path)
    if path.suffix.lower() != ".docx" or not path.exists():
        return None
    try:
        document = DocxDocument(str(path))
    except Exception:                                      # noqa: BLE001
        log.exception("Could not open DOCX template %s", template_path)
        return None
    body = document.element.body
    for child in list(body):
        if not child.tag.endswith("sectPr"):
            body.remove(child)
    return document


def _column_widths(columns, usable_mm, matrix=False):
    """Column widths (mm) at the spec's measured positions.

    A face statement has four columns: the caption, the note number centred at
    337 pt, then the two amounts ending 98 pt apart. A note table with two
    amounts and a caption keeps the same amount columns, so every table's
    figures line up on the page.
    """
    from . import output_spec

    caption, note, current, prior = output_spec.statement_columns_mm()
    if matrix and columns == 4:
        # a caption and three equal figure columns: share capital, retained
        # earnings, total
        each = 30
        return [max(usable_mm - 3 * each, 40), each, each, each]
    if columns == 4:
        return [caption, note, current, prior]
    if columns == 3:
        return [max(usable_mm - current - prior, 40), current, prior]
    if columns == 2:
        return [max(usable_mm - prior, 40), prior]
    amounts = max(columns - 1, 1)
    each = min(prior, (usable_mm * 0.5) / amounts)
    return [max(usable_mm - amounts * each, 40)] + [each] * amounts


def build(html: str, title: str = None, draft: bool = False, template_path: str = None,
          page_header: tuple = None, toc_pages: dict = None) -> bytes:
    """The report's HTML as a .docx file, returned as bytes.

    `draft` stamps DRAFT - INCOMPLETE in the header of every page: accounts
    with anything incomplete can be reviewed, never issued as a clean copy.

    `template_path` is an optional path to a customer's custom template file.
    When provided:
    - If DOCX: loads the template's styling/structure and inserts our content
    - If PDF/other: converts to DOCX first, then uses its styling
    - If "STANDARD" sentinel: uses built-in template (user declined custom)
    - If None: uses built-in template (backward compat)
    """
    reader = _Reader()
    reader.feed(html)
    instructions = reader.close()

    # A customer's own template. A Word file supplies the whole document
    # shell; a PDF supplies only its measured layout, applied below to a
    # fresh document (see _pdf_look).
    document = None
    look = None
    if template_path and template_path != "STANDARD":
        document = _load_docx_template(template_path)
    keep_own_layout = document is not None
    if document is None:
        # One output format for every set (AuditMate_Output_Format_Spec.pdf).
        # A PDF template used to be measured here, which gave Times on Letter:
        # its fonts carry anonymous names, so the face was never recognised.
        from . import output_spec
        look = output_spec.look()
        document = DocxDocument()

    if not keep_own_layout:
        # A4 portrait with 20 mm margins by default. python-docx starts from
        # Word's own template, which is US Letter at one inch - so a
        # Singapore set came out on American paper. A customer's PDF template
        # overrides all of this with what it measured.
        section = document.sections[0]
        section.orientation = WD_ORIENT.PORTRAIT
        page_w, page_h = look["page_mm"] if look else (210, 297)
        section.page_width = Mm(page_w)
        section.page_height = Mm(page_h)
        for edge, key in (("left_margin", "left"), ("right_margin", "right"),
                          ("top_margin", "top"), ("bottom_margin", "bottom")):
            setattr(section, edge, Mm(look[key] if look else 20))
        # The cover carries no running header and no page number; both start
        # on page 2, so the first page has a header and footer of its own.
        section.different_first_page_header_footer = True
        _page_numbers(section, spec=True)

        header = section.header
        if page_header:
            company, registration = page_header
            first_line = header.paragraphs[0]
            first_line.alignment = WD_ALIGN_PARAGRAPH.LEFT
            first_line.add_run((company or "").upper()).bold = True
            second = header.add_paragraph()
            second.add_run(f"Company Registration No.: {registration or '—'}").italic = True
        if draft:
            for target in (header, section.first_page_header):
                paragraph = (target.paragraphs[0].insert_paragraph_before()
                             if target.paragraphs[0].text else target.paragraphs[0])
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                stamp = paragraph.add_run("DRAFT — INCOMPLETE")
                stamp.bold = True
                stamp.font.size = Pt(12)

        face, body_pt = look["face"], look["body_pt"]
        for name in ("Normal", "Header", "Footer"):
            style = document.styles[name]
            style.font.name = face
            style.font.size = Pt(body_pt)
            rfonts = style.element.get_or_add_rPr().get_or_add_rFonts()
            for attribute in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
                rfonts.set(qn(attribute), face)
        normal = document.styles["Normal"]
        # A 10.2 pt line on 9 pt type, and one blank line between paragraphs.
        normal.paragraph_format.line_spacing = Pt(look["line_pitch"])
        normal.paragraph_format.space_before = Pt(0)
        normal.paragraph_format.space_after = Pt(look["line_pitch"])

        # Word's built-in heading styles are a blue sans-serif inherited from
        # its default template. Restyled, not abandoned, to keep the navigation
        # pane and the table of contents: 9 pt bold black, as the spec has every
        # heading.
        for level in range(0, 5):
            style = document.styles["Title" if level == 0 else f"Heading {level}"]
            style.font.name = face
            style.font.size = Pt(body_pt)
            style.font.bold = True
            style.font.color.rgb = RGBColor(0, 0, 0)
            rfonts = style.element.get_or_add_rPr().get_or_add_rFonts()
            for attribute in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
                rfonts.set(qn(attribute), face)
            style.paragraph_format.keep_with_next = True
            style.paragraph_format.space_before = Pt(look["line_pitch"])
            style.paragraph_format.space_after = Pt(0)
            style.paragraph_format.line_spacing = Pt(look["line_pitch"])

    else:
        # A customer's own Word template keeps its page setup, styles,
        # header and footer. Two things are still ours to guarantee: a copy
        # with anything incomplete is stamped on EVERY page - not only in
        # the body, where it prints once - and a set of accounts has page
        # numbers, which a template's footer may not carry.
        section = document.sections[0]
        if draft:
            header = section.header
            paragraph = (header.paragraphs[0].insert_paragraph_before()
                         if header.paragraphs and header.paragraphs[0].text
                         else (header.paragraphs[0] if header.paragraphs
                               else header.add_paragraph()))
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            stamp = paragraph.add_run("DRAFT — INCOMPLETE")
            stamp.bold = True
            stamp.font.size = Pt(12)
        if not _footer_has_page_field(section):
            _page_numbers(section, own_paragraph=True)

    if title:
        document.add_heading(title, level=0)

    table = None
    table_classes = []
    pending_rows = []
    # The section anchor seen but not yet attached: it is placed on the
    # first heading that follows, which is what the contents page names.
    pending_bookmark = None
    bookmark_number = 0

    pending_format = {}

    def _apply_format(paragraph):
        """The page break, alignment and space a section asked for, applied to
        the next paragraph."""
        if pending_format.get("page_break"):
            paragraph.paragraph_format.page_break_before = True
        if pending_format.get("align") == "center":
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if pending_format.get("before"):
            paragraph.paragraph_format.space_before = Pt(pending_format["before"])
        if pending_format.get("rule_under"):
            borders = _element("w:pBdr")
            borders.append(_element("w:bottom", val="single", sz="6", space="4",
                                    color="000000"))
            paragraph._p.get_or_add_pPr().insert_element_before(
                borders, "w:shd", "w:tabs", "w:suppressAutoHyphens", "w:kinsoku",
                "w:wordWrap", "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE",
                "w:autoSpaceDN", "w:bidi", "w:adjustRightInd", "w:snapToGrid",
                "w:spacing", "w:ind", "w:contextualSpacing", "w:mirrorIndents",
                "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment",
                "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle",
                "w:rPr", "w:sectPr", "w:pPrChange")
        pending_format.clear()

    for kind, arg, payload in instructions:
        if kind == "next":
            pending_format.update(arg)
            continue

        if kind == "table_start":
            pending_rows = []
            table = True
            table_classes = arg or []
            continue

        if kind == "row" and table:
            # arg is (is_header, row_kind) - kept whole, because the kind is
            # what decides where a rule is drawn.
            pending_rows.append((arg, payload))
            continue

        if kind == "table_end":
            if pending_rows and pending_format.get("page_break"):
                lead = document.add_paragraph()
                lead.paragraph_format.space_after = Pt(0)
                _apply_format(lead)
            if pending_rows:
                width = max(len(cells) for _meta, cells in pending_rows)
                docx_table = document.add_table(rows=0, cols=width)
                docx_table.alignment = WD_TABLE_ALIGNMENT.CENTER
                docx_table.autofit = False
                _clear_table_borders(docx_table)

                # One set of column widths for every statement, so the
                # amount columns line up when a reader turns the page from
                # the balance sheet to the cash flow. The label column
                # takes what is left, which is what stops "Net cash flow
                # (used in)/generated from operating activities" wrapping
                # onto three lines.
                page = document.sections[0]
                usable = Emu(page.page_width - page.left_margin
                             - page.right_margin).mm
                amounts = max(width - 1, 1)
                label_mm = max(usable - amounts * AMOUNT_COLUMN_MM,
                               MIN_LABEL_MM)

                spec_mode = look is not None and look.get("spec")
                widths = (_column_widths(width, usable,
                                         "matrix" in (table_classes or []))
                          if spec_mode else None)

                # Words in columns - the standards not yet effective, the credit
                # risk grading - are not amounts: a wide middle column, left
                # aligned, not the two narrow figure columns.
                text_table = "grading" in (table_classes or [])
                if text_table and width == 3:
                    first = (pending_rows[0][1][0][0] or "").strip().lower()
                    share = (0.14, 0.50, 0.36) if first == "category" else (0.18, 0.57, 0.25)
                    widths = [usable * s for s in share]

                last_index = len(pending_rows) - 1
                for position, ((is_header, kind_of_row), cells) in enumerate(
                        pending_rows):
                    row = docx_table.add_row()
                    _keep_row_whole(row)
                    if is_header:
                        _repeat_as_header(row)
                    for index in range(width):
                        text, align, refs = (cells[index]
                                             if index < len(cells)
                                             else ("", None, ()))
                        cell = row.cells[index]
                        cell.width = Mm(widths[index] if widths else
                                        (label_mm if index == 0
                                         else AMOUNT_COLUMN_MM))
                        paragraph = cell.paragraphs[0]
                        if spec_mode:
                            paragraph.paragraph_format.space_after = Pt(0)
                        if refs:
                            # A contents entry: the page each section lands
                            # on. A range where the notes run over several.
                            for position_ref, target in enumerate(refs):
                                if position_ref:
                                    paragraph.add_run(" – ")
                                _page_ref(paragraph, target,
                                          (toc_pages or {}).get(target))
                        else:
                            run = paragraph.add_run(text)
                            run.bold = (is_header or kind_of_row in ("total", "subtotal")
                                        if spec_mode
                                        else (is_header or kind_of_row == "total"))
                            if spec_mode and is_header and re.fullmatch(
                                    r"\d{4}|Note", (text or "").strip()):
                                # the rule under a year heading is the width of
                                # the heading only
                                run.underline = True
                        # The column's own marked-up class wins; the regex is
                        # a fallback for a table with no such marking at all
                        # (a note an auditor typed by hand, say).
                        if not text_table and (align == "right" or (
                                align is None and NUMERIC.match(text or ""))):
                            paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                        elif align == "center":
                            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER

                        # The ruling. Under the headings, over a subtotal,
                        # and double under the last total - nowhere else.
                        amount_column = index >= width - 2
                        if spec_mode:
                            # Ruled, not gridded: above a subtotal or total in
                            # the two amount columns only, doubled under the
                            # final total. The headings carry their own
                            # underline (above).
                            if not is_header and amount_column:
                                if kind_of_row == "subtotal":
                                    _rule_over(cell)
                                elif kind_of_row == "total":
                                    _rule_over(cell)
                                    _rule_under(cell, double=position == last_index)
                        elif is_header:
                            _rule_under(cell)
                        elif kind_of_row == "subtotal":
                            _rule_over(cell)
                        elif kind_of_row == "total":
                            _rule_over(cell)
                            _rule_under(cell, double=position == last_index)
                document.add_paragraph()
            table = None
            pending_rows = []
            continue

        if kind == "bookmark":
            pending_bookmark = arg
            continue

        if kind == "heading":
            document.add_heading("", level=min(arg + 1, 4))
            _apply_format(document.paragraphs[-1])
            _write_runs(document.paragraphs[-1],
                        [(text, True, italic) for text, bold, italic in payload])
            if pending_bookmark:
                bookmark_number += 1
                _bookmark(document.paragraphs[-1], pending_bookmark,
                          bookmark_number)
                pending_bookmark = None
        elif kind == "bullet":
            bullet = document.add_paragraph(style="List Bullet")
            _apply_format(bullet)
            _write_runs(bullet, payload)
        elif kind == "rule":
            document.add_paragraph("_" * 60)
        elif kind == "para":
            paragraph = document.add_paragraph()
            _apply_format(paragraph)
            _write_runs(paragraph, payload)
            if pending_bookmark:
                bookmark_number += 1
                _bookmark(paragraph, pending_bookmark, bookmark_number)
                pending_bookmark = None

    _update_fields_on_open(document)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
