"""A statement drawn in the customer's own lines.

A customer who brings their own accounts brings their own way of setting the
statements out: Brown Rock's profit and loss runs Revenue, Cost of sales,
Gross profit, Other income, Administrative expenses, Finance cost, Profit
before income tax, Income tax expenses; its balance sheet has one "Trade and
other receivables" line, "Loan from a bank", "Total liabilities". Ours is a
fixed list of thirty-odd standard lines. This reads which lines a template's
profit and loss and balance sheet use, and draws the customer's statement in
them.

Presentation only. The statements are still BUILT from the trial balance on
the standard lines, because the cash flow, the equity statement and the notes
all read them by those names; here the finished lines are added up under the
template's labels. So a figure cannot differ between the two views, only
where it sits.

Anything the customer has that the template has no line for is appended under
its own label - the new account in a new year is exactly the thing a template
written last year cannot list - so the statement still adds up.

Conservative, like template_outline: a statement whose lines are not
recognised is left in the standard layout.
"""
import logging
import re
from decimal import Decimal
from pathlib import Path

log = logging.getLogger(__name__)

ZERO = Decimal("0")

# What a line in a printed statement is, from its wording. Order matters: the
# first that matches wins, so "total equity and liabilities" is tried before
# "total equity".
PL_TYPES = (
    ("profit_before_tax", r"before (income )?tax|^(profit|loss) before"),
    ("profit_for_year", r"^(profit|loss|net profit|net loss)( or loss)? for "
                        r"the (financial )?(year|period)|^total comprehensive"
                        r"|^net (profit|loss)"),
    ("gross_profit", r"^gross (profit|loss|margin)"),
    ("cost_of_sales", r"^cost of (sales|goods|services|revenue)"),
    ("other_income", r"^other (income|revenue|gains?)"),
    ("finance_cost", r"^(finance (cost|charge)s?|interest expenses?)"),
    ("admin_expenses", r"^(administrative|admin|general and admin|operating "
                       r"expenses)"),
    ("income_tax", r"^(income tax|tax expense|taxation)"),
    ("revenue", r"^(revenue|turnover|sales)\b"),
)

BS_TYPES = (
    ("total_equity_liabilities", r"^total (equity and liabilities|"
                                 r"liabilities and equity)"),
    ("total_liabilities", r"^total liabilities"),
    ("total_current_assets", r"^total current assets"),
    ("total_assets", r"^total assets"),
    ("total_equity", r"^total (equity|capital and reserves|shareholders)"),
    ("share_capital", r"^(share|paid.?up|issued) capital"),
    ("retained_earnings", r"^(retained (earnings|profits?)|accumulated)"),
    ("tax_payable", r"(income tax|tax) payable|provision for (income )?tax"),
    ("loan", r"^(loan|borrowing|bank loan|term loan)"),
    ("receivables", r"receivable"),
    ("cash", r"^cash"),
)

# A page's own heading lines, never a statement line.
_HEADINGS = {
    "assets", "current assets", "non-current assets", "equity and liabilities",
    "equity", "liabilities", "non-current liability", "non-current liabilities",
    "current liability", "current liabilities", "capital and reserves",
}
_SKIP_START = ("the accompanying", "for the ", "as at", "note", "s$", "sgd")

# A printed line ends in its note number and its amounts: "Revenue 4 738,845
# 695,036". All of that is stripped to leave the caption.
_AMOUNTS = re.compile(r"(?:\s+(?:\(?-?[\d,]+(?:\.\d+)?\)?|[-–]))+\s*$")


def _tidy(text):
    return " ".join(text.replace("�", "'").split())


def _norm(text):
    return re.sub(r"[^a-z0-9$ ]+", " ", text.lower()).strip()


def _statement_page(pdf, keywords, exclude):
    for page in pdf.pages[:12]:
        lines = (page.extract_text() or "").splitlines()
        head = _norm(" ".join(lines[:4]))
        if any(k in head for k in keywords) and not any(
                x in head for x in exclude):
            return lines
    return None


def _captions(lines):
    """The captions of a statement page that carry figures, in order."""
    out, pending = [], None
    for raw in lines[2:]:
        text = _tidy(raw)
        if not text:
            continue
        stripped = _AMOUNTS.sub("", text).strip()
        has_amount = stripped != text
        low = _norm(stripped)
        if not low or low in _HEADINGS or low.startswith(_SKIP_START):
            pending = None
            continue
        if not has_amount:
            # A caption that wraps: "Profit for the financial year,
            # representing total / comprehensive income ... (121,475)".
            pending = f"{pending} {stripped}" if pending else stripped
            continue
        out.append(f"{pending} {stripped}".strip() if pending else stripped)
        pending = None
    return out


def _recognise(captions, types):
    rows, seen = [], set()
    for caption in captions:
        low = _norm(caption)
        for kind, pattern in types:
            if re.search(pattern, low):
                if kind not in seen:
                    seen.add(kind)
                    rows.append({"type": kind, "label": caption})
                break
    return rows


def _mark_notes(rows, lines):
    """Say, for each recognised row, whether the template printed a note number
    against it ("Trade and other receivables  8  39,953 ..."). A row that had none
    - Income tax payable, Retained earnings - keeps none, whatever the library
    would like to refer it to."""
    for row in rows:
        want = _norm(row["label"])
        row["note"] = False
        for raw in lines:
            text = _tidy(raw)
            if _norm(text).startswith(want) and want:
                rest = text[len(row["label"]):].strip() if text.lower().startswith(
                    row["label"].lower()) else ""
                if re.match(r"^\d{1,2}\s+[\(\-\d]", rest):
                    row["note"] = True
                break


def read_profile(template_path):
    """{"profit_and_loss": [...], "balance_sheet": [...]} as the template has
    them, or {} for a template whose statements are not recognised."""
    if not template_path or template_path == "STANDARD":
        return {}
    path = Path(template_path)
    if path.suffix.lower() != ".pdf" or not path.exists():
        return {}
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            pl_lines = _statement_page(
                pdf, ("profit or loss", "comprehensive income",
                      "income statement"),
                ("cash flow", "changes in equity", "financial position"))
            bs_lines = _statement_page(
                pdf, ("financial position", "balance sheet"),
                ("cash flow", "changes in equity"))
            equity_matrix_template = _equity_page_is_matrix(pdf)
            equity_rows = _equity_template_rows(pdf) if equity_matrix_template else []
            cf_lines = _statement_page(
                pdf, ("cash flow",), ("changes in equity", "financial position"))
    except Exception:                                      # noqa: BLE001
        log.exception("Could not read the statements of %s", path)
        return {}

    profile = {}
    if equity_matrix_template:
        profile["changes_in_equity"] = [{"type": "matrix", "rows": equity_rows}]
    if pl_lines:
        rows = _recognise(_captions(pl_lines), PL_TYPES)
        kinds = {r["type"] for r in rows}
        if "revenue" in kinds and "profit_before_tax" in kinds \
                and len(rows) >= 5:
            _mark_notes(rows, pl_lines)
            profile["profit_and_loss"] = rows
    if bs_lines:
        rows = _recognise(_captions(bs_lines), BS_TYPES)
        kinds = {r["type"] for r in rows}
        if {"cash", "total_assets", "total_equity_liabilities"} <= kinds \
                and len(rows) >= 6:
            _mark_notes(rows, bs_lines)
            profile["balance_sheet"] = rows
            profile["balance_sheet_headings"] = _bs_headings(bs_lines)
    if cf_lines:
        wording = _cash_flow_wording(cf_lines)
        if wording:
            profile["cash_flow_wording"] = wording
    return profile


_CF_LABELS = (
    (r"^net cash (provided|generated).*operating", "cf_operating_total"),
    (r"^net cash.*investing", "cf_investing_total"),
    (r"^net cash.*financing", "cf_financing_total"),
    (r"^net change in cash", "cf_net_change"),
    (r"^cash and cash equivalents at beginning", "cf_opening_cash"),
    (r"^cash and cash equivalents at end", "cf_closing_cash"),
)


def _cash_flow_wording(lines):
    """The template's own captions for the cash flow's headings and totals.

    Only wording is taken: the figures stay the computed ones, so a line whose
    meaning differs (the template starts from profit after tax; ours from profit
    before tax) keeps our label rather than take one that misdescribes it.
    """
    labels, headings, found = {}, {}, []
    for raw in lines[2:]:
        text = _tidy(raw)
        text = re.sub(r"\s+\(?[\d,.]+\)?(\s+\(?[\d,.]+\)?)*$", "", text).strip()
        low = _norm(text)
        if low == "operating activities":
            headings["operating"] = text
            found.append("operating")
        elif low == "investing activities":
            headings["investing"] = text
            found.append("investing")
        elif low == "financing activities":
            headings["financing"] = text
            found.append("financing")
        for pattern, key in _CF_LABELS:
            if re.match(pattern, low) and key not in labels:
                labels[key] = text
    if not labels and not headings:
        return None
    defaults = {"operating": "Operating activities",
                "investing": "Investing activities",
                "financing": "Financing activities"}
    return {"labels": labels, "headings": {**defaults, **headings},
            "found": found}


def _bs_headings(lines):
    """The captions the template puts over its groups, in its own wording:
    "ASSETS", "Current assets", "EQUITY AND LIABILITIES", "Equity",
    "Non-current liability", "Current liability" - singular where it says so."""
    found = {}
    for raw in lines[2:]:
        text = _tidy(raw)
        low = _norm(text)
        if low == "assets":
            found["assets"] = text
        elif low == "current assets":
            found["current_assets"] = text
        elif low == "equity and liabilities":
            found["equity_major"] = text
        elif low in ("equity", "capital and reserves", "shareholders equity"):
            found["equity"] = text
        elif low in ("non current liability", "non current liabilities"):
            found["non_current_liabilities"] = text
        elif low in ("current liability", "current liabilities"):
            found["current_liabilities"] = text
    return found


def group_headings(template_headings, presented):
    """The balance sheet's group captions, from the template's wording.

    "ASSETS" heads the first group of assets whichever it is: the standard
    layout only printed it over non-current assets, so a company with none
    (Brown Rock) had no ASSETS caption at all.
    """
    t = template_headings or {}
    groups = {getattr(line, "group_key", None) for line in presented or []}
    assets = t.get("assets")
    current = t.get("current_assets", "Current assets")
    out = {}
    if "non_current_assets" in groups:
        out["non_current_assets"] = (f"{assets}|Non-current assets" if assets
                                     else "Non-current assets")
        out["current_assets"] = current
    else:
        out["current_assets"] = f"{assets}|{current}" if assets else current
    equity = t.get("equity", "Capital and reserves")
    out["equity"] = (f"{t['equity_major']}|{equity}" if t.get("equity_major")
                     else f"EQUITY AND LIABILITIES|{equity}")
    out["non_current_liabilities"] = t.get("non_current_liabilities",
                                            "Non-current liabilities")
    out["current_liabilities"] = t.get("current_liabilities",
                                        "Current liabilities")
    return out


# ------------------------------------------------------------------ drawing

class PresentedLine:
    """A row of a drawn statement, shaped like a StatementLine for the
    template that renders it - but with no id, so nothing on the page tries
    to edit it. The lines it is added up from stay editable on the standard
    statement."""

    def __init__(self, key, label, current, previous, *, group,
                 total=False, subtotal=False, note=None, indent=1):
        self.id = None
        self.line_key = key
        self.label = self.effective_label = label
        self.label_is_overridden = False
        self.group_key = group
        self.indent = indent
        self.is_total = total
        self.is_subtotal = subtotal
        self.is_detail = False
        self.is_computed = True
        self.is_overridden = False
        self.note_ref = note
        self.amount_current = self.effective_amount = current
        self.amount_previous = previous
        self.manual_override_amount = None
        self.override_record = None
        self.source_line_item_ids = ()
        self.no_prior_entry = True


def _cur(line):
    return Decimal(str(line.effective_amount or 0)) if line else ZERO


def _prev(line):
    if line is None or line.amount_previous is None:
        return None
    return Decimal(str(line.amount_previous))


def _agg(book, keys, sign=1):
    """This year's sum of some lines, and last year's when it is known."""
    current, previous = ZERO, None
    for key in keys:
        line = book.get(key)
        if line is None:
            continue
        current += _cur(line)
        value = _prev(line)
        if value is not None:
            previous = (previous or ZERO) + value
    return sign * current, (None if previous is None else sign * previous)


def _note(book, *keys):
    for key in keys:
        line = book.get(key)
        if line is not None and getattr(line, "note_ref", None):
            return line.note_ref
    return None


def _nonzero(current, previous):
    return bool(current) or bool(previous)


def _labelled(rows, kind, default):
    for row in rows:
        if row["type"] == kind:
            return row["label"]
    return default


def _is_derived(line):
    return bool(line.formula) or line.is_subtotal or line.is_total


def present(statement, rows):
    """The statement's lines under the template's captions, or None when it
    cannot be drawn that way (the caller then draws the standard one)."""
    if statement is None or not rows:
        return None
    book = {line.line_key: line for line in statement.lines}
    if statement.statement_type == "profit_and_loss":
        drawn = _present_profit_and_loss(book, rows)
    elif statement.statement_type == "balance_sheet":
        drawn = _present_balance_sheet(book, rows)
    else:
        return None
    # The same rule as the standard statements: a line with nothing in it in
    # either year is left off. The customer's template lists every line it
    # ever needed - Brown Rock's had "Income tax expenses (0)" because last
    # year had some - and printing a row of dashes for a company with no tax
    # in either year only asks the reader what is missing. Subtotals and
    # totals always stay, since they anchor the statement.
    # The notes column is the template's: a line it printed no note against
    # prints none here.
    unnoted = {r["type"] for r in rows if r.get("note") is False}
    for row in drawn:
        if row.line_key in unnoted:
            row.note_ref = None
    # ... except a line the template printed a note number against: the note
    # is part of the set, and the face has to point at it. Brown Rock's "Income
    # tax expenses  7" is nil in both years and still the only line that tells
    # a reader Note 7 is about the tax charge.
    noted = {r["type"] for r in rows if r.get("note") is True}
    alias = {"tax_expense": "income_tax"}
    return [row for row in drawn
            if row.is_total or row.is_subtotal
            or row.amount_current or row.amount_previous
            or alias.get(row.line_key, row.line_key) in noted]


def _present_profit_and_loss(book, rows):
    from .classify import _index

    have = {r["type"] for r in rows}
    income = {"other_income", "interest_income", "iras_rebate"}
    finance = {"interest_expense"}
    opex = [key for key, entry in _index().items()
            if entry.get("group") == "operating_expenses" and key in book
            and not _is_derived(book[key])]

    show_other = "other_income" in have
    show_finance = "finance_cost" in have
    admin = [k for k in opex
             if not (show_other and k in income)
             and not (show_finance and k in finance)]

    def add(out, key, label, values, **kw):
        out.append(PresentedLine(key, label, values[0], values[1],
                                 group="pl_flat", **kw))

    out = []
    add(out, "revenue", _labelled(rows, "revenue", "Revenue"),
        _agg(book, ["revenue"]), note=_note(book, "revenue"))
    add(out, "cost_of_sales", _labelled(rows, "cost_of_sales", "Cost of sales"),
        _agg(book, ["cost_of_sales"], -1))
    add(out, "gross_profit", _labelled(rows, "gross_profit", "Gross profit"),
        _agg(book, ["gross_profit"]), subtotal=True)
    if show_other:
        add(out, "other_income", _labelled(rows, "other_income", "Other income"),
            _agg(book, [k for k in opex if k in income], -1),
            note="other_income")
    add(out, "admin_expenses",
        _labelled(rows, "admin_expenses", "Administrative expenses"),
        _agg(book, admin, -1), note=_note(book, "operating_expenses"))
    if show_finance:
        add(out, "finance_cost", _labelled(rows, "finance_cost", "Finance cost"),
            _agg(book, [k for k in opex if k in finance], -1),
            note="finance_costs")
    add(out, "profit_before_tax",
        _labelled(rows, "profit_before_tax", "Profit before tax"),
        _agg(book, ["profit_before_tax"]), subtotal=True,
        note=_note(book, "profit_before_tax"))
    tax = _agg(book, ["tax_expense"], -1)
    if "income_tax" in have or _nonzero(*tax):
        add(out, "tax_expense", _labelled(rows, "income_tax",
                                          "Income tax expense"),
            tax, note=_note(book, "tax_expense") or "income_tax_expense")
    label = _labelled(rows, "profit_for_year",
                      "Profit for the period, net of tax")
    key = ("total_comprehensive_income" if "comprehensive" in label.lower()
           else "profit_for_year")
    add(out, "profit_for_year", label, _agg(book, [key]), total=True)
    return out


CURRENT_ASSET_KEYS = {"trade_receivables", "prepayments", "contract_assets"}


def _present_balance_sheet(book, rows):
    have = {r["type"] for r in rows}

    def line_for(kind, default, keys, group, **kw):
        current, previous = _agg(book, keys)
        return PresentedLine(kind, _labelled(rows, kind, default), current,
                             previous, group=group, **kw)

    receivables = line_for(
        "receivables", "Trade and other receivables",
        ["trade_receivables", "prepayments", "contract_assets"],
        "current_assets", note=_note(book, "trade_receivables"))
    cash = line_for("cash", "Cash and cash equivalents",
                    ["cash_and_equivalents"], "current_assets",
                    note=_note(book, "cash_and_equivalents"))
    share = line_for("share_capital", "Share capital", ["share_capital"],
                     "equity", note=_note(book, "share_capital"))
    retained = line_for("retained_earnings", "Retained earnings",
                        ["retained_earnings"], "equity",
                        note=_note(book, "retained_earnings") or "reserves")
    loan = line_for("loan", "Loan from a bank", ["long_term_borrowings"],
                    "non_current_liabilities",
                    note=_note(book, "long_term_borrowings"))
    tax_payable = line_for("tax_payable", "Income tax payable",
                           ["tax_payable"], "current_liabilities",
                           note=_note(book, "tax_payable")
                           or "income_tax_expense")

    consumed = {"cash_and_equivalents", "share_capital", "retained_earnings",
                "long_term_borrowings", "tax_payable"}
    consumed |= CURRENT_ASSET_KEYS

    extras = []
    for key, line in book.items():
        if key in consumed or _is_derived(line):
            continue
        current, previous = _cur(line), _prev(line)
        if not _nonzero(current, previous):
            continue
        extras.append(PresentedLine(
            key, line.effective_label, current, previous,
            group=line.group_key, note=getattr(line, "note_ref", None)))

    def group(name):
        return [e for e in extras if e.group_key == name]

    def total(kind, default, key, group_name):
        current, previous = _agg(book, [key])
        return PresentedLine(kind, _labelled(rows, kind, default), current,
                             previous, group=group_name, total=(
                                 kind in ("total_assets",
                                          "total_equity_liabilities")),
                             subtotal=kind not in ("total_assets",
                                                   "total_equity_liabilities"))

    out = []
    non_current = group("non_current_assets")
    if non_current:
        out += non_current
        current, previous = _agg(book, ["total_non_current_assets"])
        out.append(PresentedLine("total_non_current_assets",
                                 "Total non-current assets", current, previous,
                                 group="non_current_assets", subtotal=True))
    out += [receivables, cash] + group("current_assets")
    out.append(total("total_current_assets", "Total current assets",
                     "total_current_assets", "current_assets"))
    out.append(total("total_assets", "Total assets", "total_assets",
                     "assets_total"))
    out += [share] + group("equity") + [retained]
    out.append(total("total_equity", "Total equity", "total_equity", "equity"))
    out += [loan] + group("non_current_liabilities")
    out += [tax_payable] + group("current_liabilities")

    nc, nc_prev = _agg(book, ["total_non_current_liabilities"])
    cl, cl_prev = _agg(book, ["total_current_liabilities"])
    both_known = nc_prev is not None or cl_prev is not None
    out.append(PresentedLine(
        "total_liabilities", _labelled(rows, "total_liabilities",
                                       "Total liabilities"),
        nc + cl, ((nc_prev or ZERO) + (cl_prev or ZERO)) if both_known
        else None, group="liabilities_subtotal", subtotal=True))
    out.append(total("total_equity_liabilities", "Total equity and liabilities",
                     "total_equity_and_liabilities", "liabilities_total"))
    return out


# ------------------------------------------------- changes in equity, as a matrix

def _equity_page_is_matrix(pdf):
    """Whether the template's equity statement is the columnar kind: Share
    capital | Retained earnings | Total, one row per date."""
    for page in pdf.pages[:12]:
        lines = (page.extract_text() or "").splitlines()
        head = _norm(" ".join(lines[:4]))
        if "changes in equity" not in head:
            continue
        body = " ".join(_norm(l) for l in lines[3:12])
        rows = sum(1 for l in lines if re.match(r"^\s*at \d", l, re.IGNORECASE))
        return ("retained" in body and "share" in body) and rows >= 2
    return False


def equity_matrix(statement, financial_year, template_rows=None):
    """The statement of changes in equity as the template lays it out.

    Share capital, retained earnings and total across, a row for each date and
    each year's total comprehensive income down: "At 1 January 2024", "Total
    comprehensive income for the year", "At 31 December 2024" ... Built from
    the same equity lines as the standard statement, so no figure can differ.

    Where this year's opening balance is not last year's closing balance the
    matrix says so in a row of its own - "Adjustment to opening balance" - so
    the columns add down and the difference is where a reader can see it,
    rather than being absorbed into a total.
    """
    from decimal import Decimal

    book = {line.line_key: line for line in statement.lines}

    def value(key, current):
        line = book.get(key)
        if line is None:
            return None
        raw = line.effective_amount if current else line.amount_previous
        return None if raw is None else Decimal(str(raw))

    def triple(prefix, current):
        share = value(f"soce_{prefix}_share", current)
        accum = value(f"soce_{prefix}_accum", current)
        total = value(f"soce_{prefix}_total", current)
        if total is None and share is not None and accum is not None:
            total = share + accum
        return share, accum, total

    def day(d):
        return f"{d.day} {d.strftime('%B %Y')}"

    start, end = financial_year.start_date, financial_year.end_date
    previous_start = start.replace(year=start.year - 1) if start else None
    previous_end = getattr(financial_year, "previous_period_end", None) or (
        end.replace(year=end.year - 1) if end else None)

    rows = []

    def add(label, share, accum, total, bold=False):
        rows.append({"label": label, "cells": [share, accum, total],
                     "bold": bold})

    def continues(template_rows, closing_total):
        """Whether the template's last row is last year's closing balance - the
        only case its rows can be carried as they stand."""
        if not template_rows:
            return False
        last = template_rows[-1]
        return (last["label"].lower().startswith("at ")
                and closing_total is not None
                and abs(Decimal(last["cells"][2]) - closing_total) < 1)

    zero = Decimal("0")
    have_previous = value("soce_close_total", False) is not None
    open_p, close_p = triple("open", False), triple("close", False)
    open_c, close_c = triple("open", True), triple("close", True)

    carried = continues(template_rows, close_p[2])
    if carried:
        for row in template_rows:
            add(row["label"], *[Decimal(c) for c in row["cells"]],
                bold=row["label"].lower().startswith("at "))
    elif have_previous and open_p[2] is not None:
        add(f"At {day(previous_start)}", *open_p, bold=True)
        if not carried:
            tci = value("soce_income_accum", False)
            issued = value("soce_issue_share", False)
            if issued:
                add("Shares issued during the year", issued, zero, issued)
            add("Total comprehensive income for the year", zero, tci, tci)
            add(f"At {day(previous_end)}", *close_p, bold=True)
    else:
        previous_end = None

    gap = None
    if have_previous and open_c[2] is not None and close_p[2] is not None:
        gap = tuple((c or zero) - (p or zero) for c, p in zip(open_c, close_p))
        if all(abs(g) < Decimal("0.5") for g in gap):
            gap = None
    if gap:
        add("Adjustment to opening balance", *gap)
    elif not have_previous:
        add(f"At {day(start)}", *open_c, bold=True)

    issued = value("soce_issue_share", True)
    if issued:
        add("Shares issued during the year", issued, zero, issued)
    add("Total comprehensive income for the year", zero,
        value("soce_income_accum", True), value("soce_income_accum", True))
    add(f"At {day(end)}", *close_c, bold=True)
    return {"columns": ["Share capital", "Retained earnings", "Total"],
            "rows": rows, "gap": gap}


# ------------------------------------------------- cash flow, in the template's layout

def cash_flow_layout(financial_year, wording=None, entry=False):
    """The cash flow as the template lays it out, from the figures already
    computed for the standard cash flow, so no figure can differ from it.

    Operating (profit after taxation, adjustments for non-cash items, changes
    in operating assets and liabilities, operating cash flows), Investing,
    Financing, Net Cash Flows, then the cash section. The template's sub-lines
    are kept even where nil.

    Interest and tax are added back and then taken out again as cash paid, as
    the template does; the two cancel, so the operating total is the standard
    statement's own. What the standard statement could not explain (an opening
    balance that is not last year's closing balance) is one row of its own,
    "Adjustment to opening balance", so the columns add - the same row the
    statement of changes in equity shows.
    """
    from decimal import Decimal

    from ..models import FinancialStatement

    template_rows = (wording or {}).get("rows")
    if template_rows:
        from . import template_note_tables
        try:
            drawn = template_note_tables.cash_flow_from_template(
                financial_year, template_rows, entry=entry)
        except Exception:                                  # noqa: BLE001
            log.exception("Could not draw the cash flow from the accounts")
            drawn = None
        if drawn:
            return drawn

    def lines(kind):
        st = FinancialStatement.query.filter_by(
            financial_year_id=financial_year.id, statement_type=kind).first()
        return {l.line_key: l for l in st.lines} if st else {}

    cf, pl = lines("cash_flow"), lines("profit_and_loss")
    if not cf:
        return None
    zero = Decimal("0")

    def get(book, key, current):
        line = book.get(key)
        if line is None:
            return None
        raw = line.effective_amount if current else line.amount_previous
        return None if raw is None else Decimal(str(raw))

    def pair(book, key):
        return (get(book, key, True), get(book, key, False))

    def neg(values):
        return tuple(None if v is None else -v for v in values)

    def plus(*pairs):
        out = []
        for i in (0, 1):
            vals = [p[i] for p in pairs if p[i] is not None]
            out.append(sum(vals, zero) if vals else None)
        return tuple(out)

    rows = []

    def head(label):
        rows.append({"label": label, "kind": "head", "cells": None,
                     "major": bool(re.match(
                         r"(?i)^(operating|investing|financing) activities$", label))})

    def item(label, values, indent=1):
        rows.append({"label": label, "kind": "item", "cells": values,
                     "indent": indent})

    def total(label, values, final=False):
        rows.append({"label": label, "kind": "total" if final else "sub",
                     "cells": values})

    profit = pair(pl, "profit_for_year")
    interest = pair(pl, "interest_expense")
    tax = pair(pl, "tax_expense")
    depreciation = pair(cf, "cf_depreciation")
    receivables, payables = pair(cf, "cf_receivables"), pair(cf, "cf_payables")
    tax_paid, expenses = pair(cf, "cf_tax_paid"), pair(cf, "cf_expenses_paid")

    words = (wording or {}).get("headings") or {}
    found = (wording or {}).get("found")
    if found is None or "operating" in found:
        head(words.get("operating", "Operating activities"))
    item("Profit after taxation", profit, 0)
    head("Adjustments for non-cash items")
    item("Interest expense", interest)
    item("Tax expense", tax)
    if any(v for v in depreciation if v):
        item("Depreciation", depreciation)
    head("Changes in operating assets and liabilities")
    item("Trade and other receivables", receivables)
    item("Trade and other payables", payables)
    head("Operating cash flows")
    item("Interest expense", neg(interest))
    item("Tax paid", tax_paid)
    if any(v for v in expenses if v):
        item("Expenses paid", expenses)
    operating = plus(profit, interest, tax, depreciation, receivables, payables,
                     neg(interest), tax_paid, expenses)
    total("Net cash provided by operating activities", operating)

    head(words.get("investing", "Investing activities"))
    purchase, other_inv = pair(cf, "cf_purchase_ppe"), pair(cf, "cf_investing_other")
    item("Purchase of fixed assets", purchase)
    item("Other cash items from investing activities", other_inv)
    investing = plus(purchase, other_inv)
    total("Net cash provided by investing activities", investing)

    head(words.get("financing", "Financing activities"))
    borrowing = pair(cf, "cf_borrowings")
    item("Proceeds from long-term loans",
         tuple(None if v is None else max(v, zero) for v in borrowing))
    item("Repayment of long-term loans",
         tuple(None if v is None else min(v, zero) for v in borrowing))
    shares, dividends = pair(cf, "cf_share_capital"), pair(cf, "cf_dividends")
    item("Proceeds from issue of shares", shares)
    item("Dividends paid", dividends)
    financing = plus(borrowing, shares, dividends)
    total("Net cash provided by financing activities", financing)

    net = plus(operating, investing, financing)
    total("Net Cash Flows", net)
    change = pair(cf, "cf_net_change")
    gap = tuple(None if c is None else c - (n or zero) for c, n in zip(change, net))
    # a difference under one dollar is rounding, not an adjustment
    gap = tuple(g if g is not None and abs(g) >= 1 else None for g in gap)
    if any(g is not None for g in gap):
        item("Adjustment to opening balance", gap, 0)

    head("Cash and cash equivalents")
    item("Cash and cash equivalents at beginning of period",
         pair(cf, "cf_opening_cash"), 0)
    item("Net change in cash for period", change, 0)
    total("Cash and cash equivalents at end of period",
          pair(cf, "cf_closing_cash"), final=True)
    return {"rows": rows}


_MONTH_CASE = re.compile(r"\b(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|"
                         r"SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\b")
_EQ_FIGURE = r"(-?[\d,]+(?:\.\d+)?|-|\u2014)"
_EQ_ROW = re.compile(r"^(?P<label>.*?\S)\s+" + _EQ_FIGURE + r"\s+" + _EQ_FIGURE
                     + r"\s+" + _EQ_FIGURE + r"\s*$")


def _equity_template_rows(pdf):
    """The template's own rows of the equity statement, as printed:
    [{"label", "cells": [share, retained, total]}] - "At 1 January 2022",
    "Total comprehensive income for the year", "At 31 December 2022" ...

    Every year the template already shows stays a row of the new statement;
    the new year is added under them. Figures are read whatever the grouping
    ("1,00,000") or sign style ("-1,21,474"); a dash is nil."""
    from decimal import Decimal

    for page in pdf.pages[:12]:
        lines = (page.extract_text() or "").splitlines()
        if "changes in equity" not in _norm(" ".join(lines[:4])):
            continue
        rows = []
        for raw in lines[3:]:
            match = _EQ_ROW.match(_tidy(raw))
            if not match or not re.match(r"(?i)^(at |total comprehensive|"
                                         r"shares? issued|dividend|issue of)",
                                         match.group("label")):
                continue
            cells = []
            for i in (2, 3, 4):
                text = match.group(i)
                if text in ("-", "\u2014"):
                    cells.append(Decimal("0"))
                else:
                    cells.append(Decimal(text.replace(",", "")))
            label = _MONTH_CASE.sub(lambda m: m.group(1).capitalize(),
                                    match.group("label"))
            rows.append({"label": label, "cells": [str(c) for c in cells]})
        return rows
    return []
