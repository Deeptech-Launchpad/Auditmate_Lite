"""AuditMate's output format - one format for every set the engine produces.

Taken from AuditMate_Output_Format_Spec.pdf ("Build to this; nothing here is
optional"), which was measured from Brown Rock FY2024 and checked against four
other sets. The preview, the PDF and the Word file all read THIS module, so
they cannot disagree about what a set of accounts looks like.

What the spec fixes: the page, the typeface and sizes, the spacing, the page
furniture, the cover, and the ruling of the tables. What it deliberately does
not: a customer's own structure and wording - that comes from their template.

Two things in the spec's source files are not copied, and the spec says why:
Brown Rock is US Letter, "a Word default rather than a choice", so the page is
A4; and its cash flow carried shaded subtotal rows pasted in from accounting
software, so there is no shading anywhere.
"""

PT_PER_MM = 72 / 25.4


def _mm(points):
    return points / PT_PER_MM


# ---- page ------------------------------------------------------------------
PAGE_PT = (595, 842)                       # A4
MARGIN_PT = {"left": 101, "right": 66, "top": 49, "bottom": 48}

# ---- type ------------------------------------------------------------------
FACE = "Arial"                             # no second typeface anywhere
FACE_STACK = '"Arial", Helvetica, sans-serif'
BODY_PT = 9                                # body, captions, figures
CASHFLOW_HEAD_PT = 9.9                     # cash flow section headings, bold
FOOTNOTE_PT = 8                            # "The accompanying notes..." italic
SHAREHOLDING_HEAD_PT = 7.5                 # directors' shareholdings heads

# ---- spacing ---------------------------------------------------------------
LINE_PITCH_PT = 10.2                       # on 9 pt type: about 1.13 lines
PARAGRAPH_GAP_PT = 20.5                    # one blank line between paragraphs
INDENT_PT = 12                             # per level in the face statements

# ---- rules (weights in points) ----------------------------------------------
RULE_HEADING_PT = 0.96                     # under a year heading / the word Note
RULE_TOTAL_PT = 0.96                       # above a subtotal or total
RULE_FINAL_PT = 0.72                       # below the final total, doubled
RULE_FINAL_GAP_PT = 1.4
RULE_TITLE_PT = 0.6                        # under a statement title

# ---- columns of a face statement, from the left margin (Letter positions
# in the spec; kept relative to the text block so A4 lines up the same) ------
NOTE_CENTRE_FROM_LEFT_PT = 337 - 101       # note number centred here
CURRENT_RIGHT_FROM_RIGHT_PT = 546 - 448    # current-year right edge, from the
#                                            prior-year (block) right edge


def text_block_pt():
    return PAGE_PT[0] - MARGIN_PT["left"] - MARGIN_PT["right"]


def statement_columns_mm():
    """(caption, note, current, prior) widths of a face statement, in mm.

    The note number is centred 236 pt from the margin; the current-year figure
    ends 98 pt before the prior-year one, which ends at the right margin.
    """
    block = text_block_pt()
    note_width = 42
    caption = NOTE_CENTRE_FROM_LEFT_PT - note_width / 2
    prior = CURRENT_RIGHT_FROM_RIGHT_PT
    current = block - caption - note_width - prior
    return tuple(round(_mm(v), 1) for v in (caption, note_width, current, prior))


def look():
    """The look dict the templates and the Word export already speak."""
    return {
        "page_mm": (_mm(PAGE_PT[0]), _mm(PAGE_PT[1])),
        "left": _mm(MARGIN_PT["left"]), "right": _mm(MARGIN_PT["right"]),
        "top": _mm(MARGIN_PT["top"]), "bottom": _mm(MARGIN_PT["bottom"]),
        "body_pt": BODY_PT,
        "face": FACE,
        "stack": FACE_STACK,
        "line_pitch": LINE_PITCH_PT,
        "spec": True,
    }
