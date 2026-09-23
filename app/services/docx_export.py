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
            anchor = next((v for k, v in attrs if k == "id"), "") or ""
            if anchor.startswith("sec-"):
                self.out.append(("bookmark", anchor, None))
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


def _bookmark(paragraph, name, number):
    """Mark this paragraph so a page reference can point at it."""
    start = _element("w:bookmarkStart", id=str(number), name=name)
    end = _element("w:bookmarkEnd", id=str(number))
    paragraph._p.insert(0, start)
    paragraph._p.append(end)


def _page_ref(paragraph, bookmark):
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
    instr.text = " PAGEREF %s \\h " % bookmark
    separate = _element("w:fldChar", fldCharType="separate")
    placeholder = OxmlElement("w:t")
    placeholder.text = "1"
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


def _load_template(template_path: str):
    """Load a customer's custom template file as a DocxDocument.

    Handles multiple formats:
    - .docx: Load directly
    - .pdf: Convert to .docx first using pdf2docx library
    - Other formats: Attempt conversion or fall back to standard template

    Returns a DocxDocument with the template's styling/structure intact.
    """
    from pathlib import Path

    path = Path(template_path)
    if not path.exists():
        log.warning(f"Custom template not found: {template_path}, using standard")
        return DocxDocument()

    suffix = path.suffix.lower()

    if suffix == ".docx":
        # Load DOCX template directly
        try:
            return DocxDocument(str(path))
        except Exception as e:
            log.error(f"Failed to load DOCX template {template_path}: {e}")
            return DocxDocument()

    elif suffix == ".pdf":
        # Convert PDF to DOCX first
        try:
            from pdf2docx import Converter
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
                tmp_docx = tmp.name

            cv = Converter(str(path))
            cv.convert(tmp_docx)
            cv.close()

            doc = DocxDocument(tmp_docx)
            Path(tmp_docx).unlink(missing_ok=True)
            return doc
        except ImportError:
            log.warning("pdf2docx not installed, cannot convert PDF template")
            return DocxDocument()
        except Exception as e:
            log.error(f"Failed to convert PDF template {template_path}: {e}")
            return DocxDocument()

    else:
        log.warning(f"Unsupported template format {suffix}, using standard")
        return DocxDocument()


def build(html: str, title: str = None, draft: bool = False, template_path: str = None) -> bytes:
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

    # Load custom template if provided
    if template_path and template_path != "STANDARD":
        document = _load_template(template_path)
        is_custom = True

        # We only want the custom template's margins, styles, headers and footers.
        # We DO NOT want the old document's text/tables clogging up the report.
        # Clear the document body but keep the final section properties (sectPr).
        body = document.element.body
        for child in list(body):
            if not child.tag.endswith('sectPr'):
                body.remove(child)
    else:
        document = DocxDocument()
        is_custom = False

    # For standard or no template, apply default page setup and styles.
    # Custom templates should retain their own layout, margins, and heading
    # styles, so we skip these adjustments when a custom template is loaded.
    if not is_custom:
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
    # The section anchor seen but not yet attached: it is placed on the
    # first heading that follows, which is what the contents page names.
    pending_bookmark = None
    bookmark_number = 0

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
                        text, align, refs = (cells[index]
                                             if index < len(cells)
                                             else ("", None, ()))
                        cell = row.cells[index]
                        cell.width = Mm(label_mm if index == 0
                                        else AMOUNT_COLUMN_MM)
                        paragraph = cell.paragraphs[0]
                        if refs:
                            # A contents entry: the page each section lands
                            # on. A range where the notes run over several.
                            for position_ref, target in enumerate(refs):
                                if position_ref:
                                    paragraph.add_run(" – ")
                                _page_ref(paragraph, target)
                        else:
                            run = paragraph.add_run(text)
                            run.bold = is_header or kind_of_row == "total"
                        # The column's own marked-up class wins; the regex is
                        # a fallback for a table with no such marking at all
                        # (a note an auditor typed by hand, say).
                        if align == "right" or (align is None
                                                and NUMERIC.match(text or "")):
                            paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                        elif align == "center":
                            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER

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

        if kind == "bookmark":
            pending_bookmark = arg
            continue

        if kind == "heading":
            document.add_heading("", level=min(arg + 1, 4))
            _write_runs(document.paragraphs[-1], payload)
            if pending_bookmark:
                bookmark_number += 1
                _bookmark(document.paragraphs[-1], pending_bookmark,
                          bookmark_number)
                pending_bookmark = None
        elif kind == "bullet":
            _write_runs(document.add_paragraph(style="List Bullet"), payload)
        elif kind == "rule":
            document.add_paragraph("_" * 60)
        elif kind == "para":
            _write_runs(document.add_paragraph(), payload)

    _update_fields_on_open(document)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
