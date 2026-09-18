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
from docx.shared import Mm, Pt, RGBColor

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
        # A set of accounts is ruled, not gridded: a line under the column
        # headings, one above each subtotal, a double one under the final
        # total, and nothing anywhere else. Which row is which is on the
        # <tr> in the preview already, so it is read here rather than
        # guessed from the text later.
        self._row_kind = ""

    # -- text collection --------------------------------------------------

    def _flush(self):
        # A <br> arrives as a LINE_BREAK run. Filtering on strip() alone drops
        # it, which silently joined two directors' names into one on the first
        # page of the accounts - so keep it, and drop only real whitespace.
        runs = [r for r in self._text
                if r[0].strip() or r[0] == LINE_BREAK]
        self._text = []

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
        self._text.append((re.sub(r"\s+", " ", data),
                           bool(self._bold), bool(self._italic)))

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
        elif tag == "br":
            self._text.append((LINE_BREAK, False, False))
        elif tag in HEADINGS:
            self._emit_block()
            self._heading = HEADINGS[tag]
        elif tag == "table":
            self._emit_block()
            self._in_table = True
            self.out.append(("table_start", None, None))
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
            self._cell_numeric = "num" in class_attr.split()
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
        elif tag in ("td", "th") and self._in_table:
            text = "".join(r[0] for r in self._flush()).strip()
            if self._row is not None:
                self._row.append((text, self._cell_numeric))
            self._cell = None
            self._cell_numeric = False
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


def _page_numbers(section):
    """PAGE of NUMPAGES, centred in the footer.

    Added as real Word fields rather than typed text, so they stay right
    when the preparer edits the document - which is the whole premise of
    delivering it in Word.
    """
    paragraph = section.footer.paragraphs[0]
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

    paragraph.add_run("Page ")
    field("PAGE")
    paragraph.add_run(" of ")
    field("NUMPAGES")
    for run in paragraph.runs:
        run.font.name = "Times New Roman"
        run.font.size = Pt(9)


def _write_runs(paragraph, runs):
    for text, bold, italic in runs:
        if text == LINE_BREAK:
            paragraph.add_run().add_break()
            continue
        run = paragraph.add_run(text)
        run.bold = bold
        run.italic = italic


def build(html: str, title: str = None, draft: bool = False) -> bytes:
    """The report's HTML as a .docx file, returned as bytes.

    `draft` stamps DRAFT - INCOMPLETE in the header of every page: accounts
    with anything incomplete can be reviewed, never issued as a clean copy.
    """
    reader = _Reader()
    reader.feed(html)
    instructions = reader.close()

    document = DocxDocument()

    # A4 portrait with 20 mm margins. python-docx starts from Word's own
    # default template, which is US Letter at one inch - so a Singapore set
    # came out on American paper with margins an inch and a quarter wide,
    # and the first thing anyone did was change the page setup by hand.
    section = document.sections[0]
    section.orientation = WD_ORIENT.PORTRAIT
    section.page_width = Mm(210)
    section.page_height = Mm(297)
    for edge in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(section, edge, Mm(20))
    _page_numbers(section)

    if draft:
        header = document.sections[0].header.paragraphs[0]
        header.alignment = 1                                   # centre
        stamp = header.add_run("DRAFT \u2014 INCOMPLETE")
        stamp.bold = True
        stamp.font.size = Pt(12)

    # Times New Roman 11pt, black - measured off the firm's own annual report
    # template rather than chosen here, and the same size the PDF is set in.
    normal = document.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(11)

    # Word's built-in heading styles are a blue sans-serif (Heading 1 is
    # 365F91, the rest 4F81BD) inherited from its default template, so every
    # heading in the delivered accounts came out blue and in the wrong face
    # while the body around it was black Times.
    #
    # Restyled rather than abandoned in favour of hand-formatted paragraphs:
    # keeping the real heading styles is what gives the document its
    # navigation pane and lets Word build a table of contents from it, which
    # a set of accounts someone is about to edit wants to keep.
    for level, size in ((0, 14), (1, 13), (2, 12), (3, 11), (4, 11)):
        style = document.styles["Title" if level == 0 else f"Heading {level}"]
        style.font.name = "Times New Roman"
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor(0, 0, 0)
        # A heading stranded at the foot of a page, with the table it
        # introduces starting the next one, was the other half of the
        # "headings move before the page" report.
        style.paragraph_format.keep_with_next = True

    if title:
        document.add_heading(title, level=0)

    table = None
    pending_rows = []

    for kind, arg, payload in instructions:
        if kind == "table_start":
            pending_rows = []
            table = True
            continue

        if kind == "row" and table:
            # arg is (is_header, row_kind) - kept whole, because the kind is
            # what decides where a rule is drawn.
            pending_rows.append((arg, payload))
            continue

        if kind == "table_end":
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
                usable = 170                                   # mm, A4 - 20/20
                amounts = max(width - 1, 1)
                label_mm = max(usable - amounts * AMOUNT_COLUMN_MM,
                               MIN_LABEL_MM)

                last_index = len(pending_rows) - 1
                for position, ((is_header, kind_of_row), cells) in enumerate(
                        pending_rows):
                    row = docx_table.add_row()
                    _keep_row_whole(row)
                    if is_header:
                        _repeat_as_header(row)
                    for index in range(width):
                        text, is_numeric = (cells[index] if index < len(cells)
                                            else ("", False))
                        cell = row.cells[index]
                        cell.width = Mm(label_mm if index == 0
                                        else AMOUNT_COLUMN_MM)
                        paragraph = cell.paragraphs[0]
                        run = paragraph.add_run(text)
                        run.bold = is_header or kind_of_row == "total"
                        # The column's own marked-up class wins; the regex is
                        # a fallback for a table with no such marking at all
                        # (a note an auditor typed by hand, say).
                        if is_numeric or NUMERIC.match(text or ""):
                            paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT

                        # The ruling. Under the headings, over a subtotal,
                        # and double under the last total - nowhere else.
                        if is_header:
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

        if kind == "heading":
            document.add_heading("", level=min(arg + 1, 4))
            _write_runs(document.paragraphs[-1], payload)
        elif kind == "bullet":
            _write_runs(document.add_paragraph(style="List Bullet"), payload)
        elif kind == "rule":
            document.add_paragraph("_" * 60)
        elif kind == "para":
            _write_runs(document.add_paragraph(), payload)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
