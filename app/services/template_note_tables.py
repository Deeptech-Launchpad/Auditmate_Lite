"""The tables inside the customer's own notes, read from their signed accounts.

A customer's note 8 is not the library's receivables table: it is their own -
"Trade receivables / - Third parties", "Other receivables / - Amount due from a
director / - Refundable deposit / - Prepayments", an unlabelled subtotal, a
total. The report follows that table. Its rows and captions are kept as
printed, last year's column is the template's own figures, this year's comes
from the books, and an account the template never had is added as a row of its
own. This module reads the tables; template_note_builder fills them.

Read by position, not by text: a table row is a line whose figures sit apart
from its caption (a sentence that happens to end in a number does not), a
caption with no figures inside a table is a heading, and a line of figures with
no caption is a subtotal.
"""
import logging
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

log = logging.getLogger(__name__)

_NOTE_HEAD = re.compile(r"^(\d+)\.\s+\S")
_AMOUNT = re.compile(r"^\(?-?\d[\d,]*(?:\.\d+)?\)?$|^[-–—]$")
_YEAR = re.compile(r"^(19|20)\d\d$")
_HEADER_WORDS = {"s$", "$", "no.", "of", "shares", "note"}
_CACHE = {}


def _number(token):
    """Decimal for a printed figure, 0 for a dash, None if it is not one."""
    if token in ("-", "–", "—"):
        return Decimal("0")
    text = token.replace(",", "")
    negative = text.startswith("(") or text.startswith("-")
    text = text.strip("()-")
    try:
        value = Decimal(text)
    except Exception:                                        # noqa: BLE001
        return None
    return -value if negative else value


def _lines(page):
    words = page.extract_words(extra_attrs=["fontname"], keep_blank_chars=False)
    # lines by position, within a few points: a figure can sit a hair above or
    # below its caption
    clusters, anchor = [], None
    for word in sorted(words, key=lambda w: w["top"]):
        if anchor is None or word["top"] - anchor > 3:
            clusters.append([])
            anchor = word["top"]
        clusters[-1].append(word)
    rows = dict(enumerate(clusters))
    out = []
    for key in sorted(rows):
        ws = sorted(rows[key], key=lambda w: w["x0"])
        merged = []
        for w in ws:
            # "(121,475)" can come out as "(1" "21,475)": one figure
            if (merged and w["x0"] - merged[-1]["x1"] < 3
                    and re.match(r"^[\(\-]?[\d,]+$", merged[-1]["text"])
                    and re.match(r"^[\d,\.]+\)?$", w["text"])
                    and merged[-1]["x0"] > 230):
                merged[-1] = {**merged[-1], "text": merged[-1]["text"] + w["text"],
                              "x1": w["x1"]}
            else:
                merged.append(w)
        ws = merged
        out.append({"words": ws, "top": ws[0]["top"], "x0": ws[0]["x0"],
                    "font": Counter(w["fontname"] for w in ws).most_common(1)[0][0]})
    return out


def _split(line):
    """(label words, [figure tokens]) - figures taken from the right, and only
    when they stand well apart from the caption."""
    words = line["words"]
    tokens = []
    index = len(words)
    while index > 0 and _AMOUNT.match(words[index - 1]["text"]) \
            and words[index - 1]["x0"] > 230:
        tokens.insert(0, words[index - 1])
        index -= 1
    label = words[:index]
    if tokens and label:
        gap = tokens[0]["x0"] - label[-1]["x1"]
        if gap < 22:
            return words, []          # a sentence ending in a number
    return label, tokens


def read_statement(path, title):
    """The rows of one face statement of the signed set, as `read` returns a
    note's table: the cash flow, say, with its group headings and account
    lines. `title` is a pattern on the page's heading."""
    tables = read(path, title=title)
    found = tables.get("S") or []
    return found[0] if found else None


def read(path, title=None):
    """{note number: [table, ...]} for a signed set's PDF.

    A table is {"header": [year tokens], "columns": n, "rows": [row]}, a row is
    {"label", "indent", "kind": "head" | "item" | "sub" | "total",
     "cells": [Decimal, ...] or None, "bold": bool}."""
    path = Path(str(path))
    if path.suffix.lower() != ".pdf" or not path.exists():
        return {}
    key = (str(path), path.stat().st_mtime, title)
    if key in _CACHE:
        return _CACHE[key]
    import pdfplumber

    try:
        with pdfplumber.open(str(path)) as pdf:
            pages = [_lines(p) for p in pdf.pages[:40]]
    except Exception:                                        # noqa: BLE001
        log.exception("Could not read the note tables of %s", path)
        return {}

    fonts = Counter()
    for lines in pages:
        for line in lines:
            for w in line["words"]:
                fonts[w["fontname"]] += 1
    body = fonts.most_common(1)[0][0] if fonts else ""

    tables, note, table = {}, None, None
    started = False
    in_notes = False
    pending = None                       # a caption line waiting for figures

    def close():
        nonlocal table, pending
        if table and any(r["kind"] in ("item", "sub", "total") for r in table["rows"]):
            tables.setdefault(note, []).append(table)
        table, pending = None, None

    for lines in pages:
        if title:
            top = " ".join(" ".join(w["text"] for w in l["words"])
                           for l in lines[:5])
            if (not re.search(title, top, re.IGNORECASE)
                    or re.search(r"table of contents", top, re.IGNORECASE)):
                continue
            in_notes, started, note = True, True, "S"
        for line in lines:
            text = " ".join(w["text"] for w in line["words"]).strip()
            if not text or re.fullmatch(r"\d{1,3}", text):
                continue
            if line["top"] < 100 and (text.isupper() or text.startswith("FOR THE")):
                if text.startswith("NOTES TO THE FINANCIAL STATEMENTS"):
                    in_notes = True
                continue                                     # running header
            if not in_notes:
                continue                # the statements come before the notes
            heading = line["font"] != body and len(text) <= 120
            if heading and _NOTE_HEAD.match(text) and "(Continued)" not in text:
                close()
                note = _NOTE_HEAD.match(text).group(1)
                started = True
                continue
            if not started:
                continue
            if heading and "(Continued)" in text:
                if table is None:
                    continue
                continue

            label_words, figure_words = _split(line)
            label = " ".join(w["text"] for w in label_words).strip()
            low = [w["text"].lower() for w in line["words"]]

            # a header line: years, "S$ S$", "No. of ... shares"
            if all(_YEAR.match(t) or t in _HEADER_WORDS for t in low):
                if not table or any(r["kind"] != "head" for r in table["rows"]):
                    close()
                    table = {"header": [t for t in low if _YEAR.match(t)],
                             "columns": 0, "rows": [], "note": note}
                elif table is not None:
                    table["header"] = table["header"] or [t for t in low if _YEAR.match(t)]
                continue

            if figure_words:
                cells = [_number(w["text"]) for w in figure_words]
                if any(c is None for c in cells):
                    close()
                    continue
                if table is None:
                    table = {"header": [], "columns": 0, "rows": [], "note": note}
                table["columns"] = max(table["columns"], len(cells))
                if pending is not None and label and label[:1].islower():
                    label = pending + " " + label            # a wrapped caption
                    table["rows"].pop()
                pending = None
                kind = "item"
                if not label:
                    kind = "sub"
                elif re.match(r"(?i)^total\b", label) or re.match(
                        r"(?i)^balance at the end", label):
                    kind = "total"
                table["rows"].append({
                    "label": label, "kind": kind, "cells": cells,
                    "indent": round(label_words[0]["x0"] if label_words else 0),
                    "bold": "bold" in line["font"].lower()})
                continue

            # a caption with no figures: a heading inside a table, else prose
            if table is not None and len(text) <= 70 and not text.endswith("."):
                if pending is not None and text[:1].islower():
                    table["rows"][-1]["label"] += " " + text
                    pending = table["rows"][-1]["label"]
                    continue
                table["rows"].append({"label": text, "kind": "head", "cells": None,
                                      "indent": round(line["x0"]),
                                      "bold": "bold" in line["font"].lower()})
                pending = text
                continue
            close()
    close()
    _CACHE[key] = tables
    return tables


# ------------------------------------------------------------------ the builder
#
# A template row is filled by what it says, in this order: the rows the
# library knows how to draw from the books (revenue, finance cost, receivables,
# cash ...); a named group of the client's accounts (professional fees are
# whatever the books call consulting, legal, accounting); and, where neither
# applies, a cell the preparer types into. Nothing is guessed: a row nobody can
# fill is Incomplete and listed.

ZERO = Decimal("0")

# (pattern on the caption in its group, how to fill it). First match wins.
_CODES = [
    (r"staff cost|employee benefit|salar|wages|payroll",
     ("codes", ["PL-STAFF", "PL-DIRFEE", "PL-CPF", "PL-LEVY"])),
    (r"legal and professional|professional fee|legal fee",
     ("accounts", r"consult|profession|legal|accounting|secretar", "PL")),
    (r"operating lease|lease expense|rental expense", ("accounts", r"lease|rent", "PL")),
    (r"interest expense|finance cost|interest on", ("codes", ["PL-FIN"])),
    (r"rendering of services|^revenue|sales|service income|contract revenue",
     ("codes", ["PL-REV"])),
    (r"due from|due to|amount due|related part|loan to", ("codes", ["BS-RPR"])),
    (r"refundable deposit|deposit", ("codes", ["BS-DEP"])),
    (r"prepayment", ("codes", ["BS-PREPAY"])),
    (r"^other receivables$|other receivables other receivables", ("codes", ["BS-OR"])),
    (r"third part|trade receivable", ("codes", ["BS-TR"])),
    (r"cash on hand|petty cash", ("accounts", r"petty|on hand", "BS-CASH")),
    (r"cash at bank|bank balance|cash and cash equivalents", ("codes", ["BS-CASH"])),
    (r"trade payable|creditor", ("codes", ["BS-TP"])),
    (r"total income tax|current year|current income tax|income tax expense",
     ("codes", ["PL-TAX"])),
    (r"profit before (income )?tax|loss before (income )?tax", ("codes", ["PL-PBT"])),
    (r"tax calculated|tax at the|tax at a", ("tax_rate",)),
    (r"stepped income exemption|partial (tax )?exemption|tax exemption",
     ("tax_exemption",)),
    (r"rebate|tax effects", ("answer", "Needs the tax computation")),
]


def _tax_row(kind, context, figures):
    """The two tax lines the template works out for itself.

    Brown Rock's 2023 note reads: profit 27,800, tax at 17% 4,726, statutory
    stepped income exemption (2,788). 17% of 27,800 is 4,726; the exemption is
    17% of 75% of the first 10,000 and 50% of the next 190,000 (7,500 + 8,900).
    Both follow the profit before tax, as the template's own figures do - a loss
    gives nil - and are for the preparer to confirm against the tax computation.
    """
    from decimal import Decimal as D

    profit = figures.resolve("PL-PBT", 0)
    if not isinstance(profit, D):
        return profit
    found = re.search(r"(\d+(?:\.\d+)?)\s*%", context)
    rate = D(found.group(1)) / D(100) if found else D("0.17")
    chargeable = max(profit, ZERO)
    if kind == "tax_rate":
        return (chargeable * rate).quantize(D("0.01"))
    exempt = (D("0.75") * min(chargeable, D(10000))
              + D("0.5") * min(max(chargeable - D(10000), ZERO), D(190000)))
    return -(exempt * rate).quantize(D("0.01"))


def _clean(label):
    return re.sub(r"^[\-–•]\s*", "", label or "").strip()


def _sum_codes(figures, codes):
    from .bindings import _is_held

    total = ZERO
    for code in codes:
        value = figures.resolve(code, 0)
        if _is_held(value):
            return value
        total += value if isinstance(value, Decimal) else ZERO
    return total


def _sum_accounts(financial_year, pattern, scope):
    """This year's balance of the accounts whose name says `pattern`."""
    from ..models import TrialBalanceAccount

    total, found = ZERO, False
    for account in TrialBalanceAccount.query.filter_by(
            financial_year_id=financial_year.id).all():
        if not re.search(pattern, account.account_name or "", re.IGNORECASE):
            continue
        if scope == "PL" and account.statement_type != "profit_and_loss":
            continue
        if scope not in ("PL", None) and scope != (account.line_code or ""):
            continue
        found = True
        net = Decimal(str(account.debit or 0)) - Decimal(str(account.credit or 0))
        total += net
    return total, found


def _fill_row(context, figures, financial_year):
    """(this year's value or Held, codes used) for a template caption."""
    from .bindings import Held

    text = " ".join(context.lower().split())
    for pattern, how in _CODES:
        if not re.search(pattern, text):
            continue
        if how[0] in ("tax_rate", "tax_exemption"):
            return _tax_row(how[0], context, figures), ["PL-PBT"]
        if how[0] == "codes":
            return _sum_codes(figures, how[1]), list(how[1])
        if how[0] == "accounts":
            value, found = _sum_accounts(financial_year, how[1], how[2])
            if how[2] not in ("PL",) and not found:
                return ZERO, []
            return value, []
        return Held(how[1]), []
    return Held("This line of last year's note has no account behind it yet; "
                "enter this year's figure"), []


def _extra_codes(figures, note_code, used, statement):
    """Lines the note covers that the books carry and no template row drew."""
    from . import conditions

    out = []
    for code in conditions.subject_codes(figures, note_code):
        if code in used or figures.lines[code].get("Statement") != statement:
            continue
        try:
            if conditions.carries_balance(figures, code):
                out.append(code)
        except Exception:                                    # noqa: BLE001
            continue
    return out


def build(spec, financial_year, statements=None):
    """One of the customer's own note tables, this year's figures in it."""
    from . import share_capital
    from .bindings import (Held, _is_held, _make_answerable, figures_for,
                           library_table)

    tables = read(spec.get("path")).get(str(spec.get("note")), [])
    index = spec.get("index", 0)
    if index >= len(tables):
        return None
    template = tables[index]
    figures = figures_for(financial_year)
    note = str(spec.get("note"))
    table_key = f"template_{note}_{index}"

    # the four-column share capital table is drawn by its own builder
    if template["columns"] >= 4:
        for lib in spec.get("library") or []:
            library = library_table(lib.get("version_id"), lib.get("table_id")) or {}
            if share_capital.is_share_capital_table(library):
                built = share_capital.table(lib, financial_year, library, figures)
                if built is not None:
                    return _template_share_rows(built, template)
        return None

    rows, used, statement = [], [], None
    head, group, block = "", [], []      # group: since the last head; block: since the last total
    tax_total = None

    end_year = getattr(financial_year.end_date, "year", None)

    def add(label, current, previous, kind, bold=False, rule=False):
        if end_year and label:
            # the bracket is the comparative: always the year before this one
            label = re.sub(r"\((20\d\d):", "(%d:" % (end_year - 1), label)
        rows.append({"label": label, "binding": "STATIC" if kind == "head" else "TEMPLATE",
                     "current": current, "previous": previous, "bold": bold,
                     "rule": rule, "ref": None, "kind": kind})

    for row in template["rows"]:
        shown_label = row["label"]                 # as the template prints it
        label = _clean(row["label"])               # for matching
        cells = row["cells"] or []
        prior = cells[0] if cells else None
        if row["kind"] == "head":
            head, group = label, []
            add(shown_label, None, None, "head", bold=row["bold"])
            continue
        if row["kind"] == "item":
            context = f"{head} {label}" if head else label
            value, codes = _fill_row(context, figures, financial_year)
            used.extend(codes)
            if codes and statement is None:
                statement = figures.lines[codes[0]].get("Statement")
            add(shown_label, value, prior, "item")
            group.append(rows[-1])
            block.append(rows[-1])
            continue
        # a subtotal or total: added up from the rows it covers, except a
        # total the books state (tax expense), which is read from them
        members = group if row["kind"] == "sub" else [
            r for r in block if r["kind"] == "item"]
        stated = None
        if row["kind"] == "total" and re.search(r"(?i)income tax", label):
            members = [r for r in members
                       if not re.search(r"(?i)profit before", r["label"])]
        current = _sum_members(members, "current")
        add(shown_label, current, prior, row["kind"], bold=row["kind"] == "total",
            rule=True)
        if row["kind"] == "total":
            block = []

    # a sum at the very end of a table that the template left uncaptioned
    # (cash at bank, then a bare line adding it up) is the table's total
    if rows and rows[-1]["kind"] == "sub" and not (rows[-1]["label"] or "").strip():
        rows[-1]["label"] = "Total"
        rows[-1]["kind"] = "total"
        rows[-1]["bold"] = True

    # A list of figures with nothing under it - no subtotal, no total, no
    # heading - is closed with a Total, as a note of amounts is. The template
    # ended some of its own lists without one (revenue, finance cost, the
    # charges in profit before tax); a reader adding the column up finds the
    # figure the note is about.
    if (not any(r["kind"] in ("sub", "total", "head") for r in rows)
            and any(r["kind"] == "item" for r in rows)):
        items = [r for r in rows if r["kind"] == "item"]
        add("Total", _sum_members(items, "current"), _sum_members(items, "previous"),
            "total", bold=True, rule=True)

    # accounts the template never had, added where the rows end
    if statement and spec.get("note_code"):
        extra = _extra_codes(figures, spec["note_code"], set(used), statement)
        if extra:
            at = next((i for i, r in enumerate(rows) if r["kind"] in ("sub", "total")),
                      len(rows))
            fresh = []
            for code in extra:
                value = figures.resolve(code, 0)
                before = figures.resolve(code, 1)
                dashed = sum(1 for r in template["rows"]
                             if r["label"].startswith("- ")) * 2 >= max(
                    1, sum(1 for r in template["rows"] if r["kind"] == "item"))
                fresh.append({"label": ("- " if dashed else "") + figures.label(code),
                              "binding": code,
                              "current": value,
                              "previous": before if isinstance(before, Decimal)
                              else ZERO,
                              "bold": False, "rule": False, "ref": None,
                              "kind": "item", "new": True})
            rows[at:at] = fresh
            # the totals below now include them
            _resum(rows)

    _make_answerable(rows, table_key, financial_year, totals=False)
    _resum(rows)

    shown = []
    for i, row in enumerate(rows):
        if row["kind"] == "head":
            shown.append(row)
            continue
        cur, prev = row["current"], row["previous"]
        empty = (not _is_held(cur) and not _is_held(prev)
                 and not (cur or ZERO) and not (prev or ZERO))
        if empty and row["kind"] == "item":
            continue                       # nil in both years: not printed
        shown.append(row)
    # a heading with nothing under it goes too
    final = []
    for i, row in enumerate(shown):
        if row["kind"] == "head":
            nxt = next((r for r in shown[i + 1:]), None)
            if nxt is None or nxt["kind"] == "head":
                continue
        final.append(row)

    for row in final:
        for column in ("current", "previous"):
            if _is_held(row[column]):
                held = row[column]
                row[f"held_{column}"] = held.reason
                row[f"held_{column}_edit"] = (
                    {"token": held.token, "field": held.field,
                     "scope": held.scope, "member": held.member}
                    if held.editable else None)
                row[column] = None
        row["from_binding"] = row.pop("binding")
        row["ids"] = row.get("ids") or []
    flags = []
    staff = next((r for r in final if r["kind"] == "item"
                  and re.search(r"(?i)staff cost", r["label"] or "")
                  and isinstance(r.get("current"), Decimal)), None)
    if staff is not None:
        names, expected = _staff_accounts(financial_year)
        if names and abs(expected - staff["current"]) >= 1:
            flags.append(
                "Staff costs in this note are {:,.0f} but the salary, CPF, "
                "allowance, levy and director's fee accounts in the trial balance "
                "add to {:,.0f} ({}). Check how these accounts are mapped."
                .format(staff["current"], expected, ", ".join(names)))
    total = next((r for r in final if r["kind"] == "total"
                  and re.search(r"(?i)income tax", r["label"])), None)
    if total is not None and isinstance(total.get("current"), Decimal):
        booked = _sum_codes(figures, ["PL-TAX"])
        if isinstance(booked, Decimal) and abs(booked - total["current"]) >= 1:
            flags.append(
                "Income tax in this note is {:,.0f} but the income statement "
                "carries {:,.0f}. Settle which is right with the tax "
                "computation.".format(total["current"], booked))
    return {"heading": None, "rows": final, "columns": None,
            "table_id": table_key, "flags": flags}


_STAFF_ACCOUNT = re.compile(
    r"(?i)salar|wage|\bcpf\b|allowance|director.{0,3}s?\W*fee|\blevy\b")


def _staff_accounts(financial_year):
    """The trial balance's own staff accounts, by their names: (names, total).
    An independent reading of the books, to set beside what the note drew."""
    from ..models import TrialBalanceAccount

    names, total = [], ZERO
    for account in TrialBalanceAccount.query.filter_by(
            financial_year_id=financial_year.id,
            statement_type="profit_and_loss").all():
        if not _STAFF_ACCOUNT.search(account.account_name or ""):
            continue
        net = Decimal(str(account.debit or 0)) - Decimal(str(account.credit or 0))
        if net:
            names.append("%s %s" % (account.account_name, "{:,.0f}".format(net)))
            total += net
    return names, total


def _sum_members(members, column):
    from .bindings import Held, _is_held

    values = [m[column] for m in members if m["kind"] == "item"]
    if any(_is_held(v) for v in values):
        return Held("The rows above it are not all known")
    if not values:
        return None
    return sum((v or ZERO for v in values), ZERO)


def _resum(rows):
    """Subtotals and totals follow their rows, this year and last where the
    template's own figure is not what the rows now add to."""
    group, block = [], []
    for row in rows:
        if row["kind"] == "head":
            group = []
        elif row["kind"] == "item":
            group.append(row)
            block.append(row)
        elif row["kind"] == "sub":
            row["current"] = _sum_members(group, "current")
        elif row["kind"] == "total":
            if row["label"]:
                members = block
                if re.search(r"(?i)income tax", row["label"]):
                    members = [r for r in block
                               if not re.search(r"(?i)profit before", r["label"])]
                row["current"] = _sum_members(members, "current")
            block = []


def _template_share_rows(built, template):
    """The template prints one row for share capital that did not move:
    "Beginning and end of financial year". Follow it when nothing was issued;
    otherwise the movement has to show, so the library's rows stay."""
    from decimal import Decimal as D

    rows = built.get("rows") or []
    if len(rows) != 3:
        return built
    issued = rows[1]["cells"]
    numeric = [c for c in issued if isinstance(c, D)]
    if numeric and any(numeric):
        return built
    label = next((r["label"] for r in template["rows"]
                  if r["kind"] == "item"), rows[2]["label"])
    caption = next((r["label"] for r in template["rows"] if r["kind"] == "head"), None)
    closing = dict(rows[2])
    closing.update({"label": label, "bold": False, "rule": False})
    built = dict(built)
    built["rows"] = ([{"label": caption, "cells": [None] * 4, "bold": True,
                       "rule": False}] if caption else []) + [closing]
    return built


# ------------------------------------------------------------------ into the report

def apply(report, template_path):
    """Swap each template note's library tables for the template's own."""
    from ..models import PriorYearNote

    financial_year = report.financial_year
    tables = read(template_path)
    if not tables:
        return 0
    prior = (PriorYearNote.query.filter_by(financial_year_id=financial_year.id)
             .order_by(PriorYearNote.id).all())
    by_key = {s.section_key: s for s in report.sections}
    changed = 0
    for position, row in enumerate(prior, start=1):
        section = by_key.get("note__" + (row.matched_key or ""))
        if section is None:
            continue
        number = str(row.note_number or position)
        binding = dict(section.data_binding or {})
        old = binding.get("note_table_specs") or []
        library = [{"version_id": s.get("version_id"), "table_id": s.get("table_id")}
                   for s in old if s.get("source") == "bindings"]
        code = next((s.get("note_code") for s in old if s.get("note_code")), None)
        binding["note_table_specs"] = [
            {"source": "template", "note": number, "index": i,
             "note_code": code, "path": str(template_path), "library": library}
            for i in range(len(tables.get(number, [])))]
        section.data_binding = binding
        changed += 1
    return changed


# ------------------------------------------------------------------ the cash flow

_ASSET_KEYS = {"trade_receivables", "prepayments", "inventories", "contract_assets",
               "other_receivables", "deposits"}
_LIABILITY_KEYS = {"trade_payables", "accruals", "tax_payable", "contract_liabilities",
                   "other_payables", "provisions"}
_BORROWING_KEYS = {"short_term_borrowings", "long_term_borrowings"}
_INVESTING_KEYS = {"ppe", "accumulated_depreciation", "intangible_assets",
                   "investment_property", "investments"}
_CASH_FLOW_TOTAL = re.compile(
    r"(?i)^(net cash|cash and cash equivalents at end)")


def _words(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower())) - {
        "the", "of", "and", "a", "an", "to", "from", "pte", "ltd"}


def _similar(a, b):
    """1.0 for the same caption, else the share of words they have in common."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def cash_flow_from_template(financial_year, template_rows, entry=False):
    """The cash flow in the customer's own layout, this year built from the
    movement of each account between the two trial balances.

    Every account except cash and equity contributes what its balance moved:
    an asset that rose is cash out, a liability that rose is cash in. Each goes
    to the template's line of the same name, else under the template's heading
    for its kind; an account the template never had gets a line of its own.
    Last year's column is the template's own figures. What the accounts cannot
    explain (an opening balance that is not last year's closing balance) is
    one row, "Adjustment to opening balance".
    """
    from ..models import FinancialStatement, TrialBalanceAccount

    accounts = TrialBalanceAccount.query.filter_by(
        financial_year_id=financial_year.id).all()
    balance_sheet = [a for a in accounts if a.statement_type == "balance_sheet"]
    if not balance_sheet or all(a.prior_debit is None and a.prior_credit is None
                                for a in balance_sheet):
        return None                       # no earlier trial balance to compare with

    def net(debit, credit):
        return Decimal(str(debit or 0)) - Decimal(str(credit or 0))

    moves = []                            # (account, cash effect, kind)
    for a in balance_sheet:
        key = a.standard_key or ""
        if key in ("cash_and_equivalents", "retained_earnings"):
            continue
        effect = -(net(a.debit, a.credit) - net(a.prior_debit, a.prior_credit))
        if not effect:
            continue
        if key in _BORROWING_KEYS:
            kind = "borrowing"
        elif key == "share_capital":
            kind = "shares"
        elif key in _INVESTING_KEYS:
            kind = "investing"
        elif key in _ASSET_KEYS:
            kind = "asset"
        elif key in _LIABILITY_KEYS:
            kind = "liability"
        else:
            kind = "asset" if net(a.debit, a.credit) > 0 else "liability"
        moves.append((a, effect, kind))

    def statement(kind):
        st = FinancialStatement.query.filter_by(
            financial_year_id=financial_year.id, statement_type=kind).first()
        return {l.line_key: l for l in st.lines} if st else {}

    pl, cf = statement("profit_and_loss"), statement("cash_flow")

    def stated(book, key, current=True):
        line = book.get(key)
        if line is None:
            return ZERO
        raw = line.effective_amount if current else line.amount_previous
        return Decimal(str(raw)) if raw is not None else ZERO

    profit, interest = stated(pl, "profit_for_year"), stated(pl, "interest_expense")
    tax = stated(pl, "tax_expense")

    # --- lay the template's rows out with this year's figures ---------------
    out, group, sections = [], "", {}

    def new_row(kind, label, cur, prior, indent=1, section=None):
        row = {"label": label, "kind": kind, "cells": (cur, prior),
               "indent": indent, "group": group, "section": section,
               "major": kind == "head" and bool(re.match(
                   r"(?i)^(operating|investing|financing) activities$", label))}
        out.append(row)
        return row

    template_items = [r for r in template_rows if r["kind"] == "item"
                      and not _CASH_FLOW_TOTAL.match(r["label"])]
    claimed = set()
    matches = {}                          # account id -> template row label (first)
    for a, effect, kind in moves:
        best, score = None, 0.0
        for r in template_items:
            s = _similar(a.account_name, r["label"])
            if s > score:
                best, score = r, s
        if best is not None and score >= 0.6 and id(best) not in claimed:
            matches[a.id] = best
            claimed.add(id(best))

    used_accounts, value_of = set(), {}
    for a, effect, kind in moves:
        row = matches.get(a.id)
        if row is not None:
            value_of[id(row)] = value_of.get(id(row), ZERO) + effect
            used_accounts.add(a.id)

    def prior_of(r):
        cells = r.get("cells") or []
        return Decimal(str(cells[0])) if cells else None

    section = "operating"
    extras = {"asset": [], "liability": [], "investing": [], "borrowing": [],
              "shares": []}
    for a, effect, kind in moves:
        if a.id not in used_accounts:
            extras[kind].append((a, effect))

    # which kind of account belongs under which of the template's headings
    def kind_of_group(label):
        low = label.lower()
        if "other current assets" in low:
            return "asset"
        if "other current liabilities" in low:
            return "liability"
        if "other cash items from investing" in low:
            return "investing"
        return None

    def flush(kind, label_of=lambda a: a.account_name, indent=1):
        for a, effect in extras[kind]:
            new_row("item", label_of(a), effect, ZERO, indent, section)
        extras[kind] = []

    group_kind = None
    for r in template_rows:
        label = r["label"]
        low = label.lower()
        ends_group = r["kind"] == "head" or _CASH_FLOW_TOTAL.match(label)             or r["kind"] in ("sub", "total")
        if ends_group and group_kind:
            flush(group_kind)
            group_kind = None
        if r["kind"] == "head":
            if "investing" in low and "other" not in low:
                flush("asset")
                flush("liability")
                section = "investing"
            elif "financing" in low and "other" not in low:
                flush("investing")
                section = "financing"
            elif "cash and cash equivalents" in low:
                section = "cash"
            group = label
            group_kind = kind_of_group(label)
            new_row("head", label, None, None, 0, section)
            continue
        if _CASH_FLOW_TOTAL.match(label) or r["kind"] in ("sub", "total"):
            if section == "operating":
                flush("asset")
                flush("liability")
            elif section == "investing":
                flush("investing")
            elif section == "financing":
                flush("borrowing")
                flush("shares", lambda a: "Proceeds from issue of shares")
            new_row("total" if low.startswith("cash and cash equivalents at end")
                    else "sub", label, None, prior_of(r), 0, section)
            continue
        # an item
        if re.match(r"(?i)^profit after tax", label):
            new_row("item", label, profit, prior_of(r), 0, section)
        elif re.match(r"(?i)^interest expense$", label) and group.lower().startswith("adjust"):
            new_row("item", label, interest, prior_of(r), 1, section)
        elif re.match(r"(?i)^tax expense$", label) and group.lower().startswith("adjust"):
            new_row("item", label, tax, prior_of(r), 1, section)
        elif re.match(r"(?i)^interest expense$", label):
            new_row("item", label, -interest, prior_of(r), 1, section)
        elif re.match(r"(?i)^tax expense$", label):
            new_row("item", label, -tax, prior_of(r), 1, section)
        elif re.match(r"(?i)^proceeds from .*loan", label):
            gain = sum((e for a, e in extras["borrowing"] if e > 0), ZERO)
            extras["borrowing"] = [(a, e) for a, e in extras["borrowing"] if e <= 0]
            new_row("item", label, gain, prior_of(r), 1, section)
        elif re.match(r"(?i)^repayment of .*loan", label):
            spent = sum((e for a, e in extras["borrowing"] if e < 0), ZERO)
            extras["borrowing"] = [(a, e) for a, e in extras["borrowing"] if e >= 0]
            new_row("item", label, spent, prior_of(r), 1, section)
        elif id(r) in value_of:
            new_row("item", label, value_of[id(r)], prior_of(r), 1, section)
        elif re.match(r"(?i)^cash and cash equivalents at beginning", label):
            new_row("item", label, None, prior_of(r), 0, section)
        elif re.match(r"(?i)^net change in cash", label):
            new_row("item", label, None, prior_of(r), 0, section)
        else:
            new_row("item", label, ZERO, prior_of(r), 1, section)

    # --- totals and the cash section ---------------------------------------
    def block_total(name):
        return sum((r["cells"][0] or ZERO for r in out
                    if r["section"] == name and r["kind"] == "item"), ZERO)

    # the two add-backs must not be double counted: interest and tax are added
    # back under "adjustments" and taken out again under "operating cash flows"
    totals = {name: block_total(name) for name in ("operating", "investing", "financing")}
    net = sum(totals.values(), ZERO)
    from .bindings import figures_for

    figures = figures_for(financial_year)
    close = figures.resolve("BS-CASH", 0)
    open_ = figures.resolve("BS-CASH", 1)
    close = close if isinstance(close, Decimal) else ZERO
    open_ = open_ if isinstance(open_, Decimal) else ZERO
    change = close - open_
    gap = change - net

    final, running = [], None
    for r in out:
        if r["kind"] == "sub":
            low = r["label"].lower()
            if "operating" in low:
                r["cells"] = (totals["operating"], r["cells"][1])
            elif "investing" in low:
                r["cells"] = (totals["investing"], r["cells"][1])
            elif "financing" in low:
                r["cells"] = (totals["financing"], r["cells"][1])
            elif low.startswith("net cash flows"):
                r["cells"] = (net, r["cells"][1])
        elif r["kind"] == "total":
            r["cells"] = (close, r["cells"][1])
        elif r["kind"] == "item" and r["label"].lower().startswith(
                "cash and cash equivalents at beginning"):
            r["cells"] = (open_, r["cells"][1])
        elif r["kind"] == "item" and r["label"].lower().startswith("net change in cash"):
            r["cells"] = (change, r["cells"][1])
        final.append(r)
        if r["kind"] == "sub" and r["label"].lower().startswith("net cash flows"):
            if abs(gap) >= 1:
                final.append({"label": "Adjustment to opening balance", "kind": "item",
                              "cells": (gap, None), "indent": 0, "group": "",
                              "section": "operating"})

    # The preparer's own entry is the statement (standard lines v8: no plug).
    # The entry form is shown every row, with the engine's figure as the
    # suggestion; what is printed is what was entered.
    from . import cash_flow_entry

    manual = cash_flow_entry.is_entered(financial_year)
    if entry or manual:
        final = [r for r in final if r["label"] != "Adjustment to opening balance"]
        cash_flow_entry.assign_keys(final)
    if entry:
        return {"rows": final}
    if manual:
        cash_flow_entry.overlay(financial_year, final)

    # nothing is printed that is nil in both years; a heading with nothing
    # under it goes with it
    shown = []
    for r in final:
        if r["kind"] == "item":
            cur, prior = r["cells"]
            if not (cur or ZERO) and not (prior or ZERO):
                continue
        shown.append(r)
    cleaned = []
    for i, r in enumerate(shown):
        if r["kind"] == "head":
            # kept while a line of figures follows it, past any headings that
            # sit directly under it ("Investing Activities" over "Other cash
            # items ..."); dropped when the next thing is a total
            j = i + 1
            while j < len(shown) and shown[j]["kind"] == "head":
                j += 1
            if j >= len(shown) or shown[j]["kind"] in ("sub", "total"):
                continue
        cleaned.append(r)
    return {"rows": cleaned}


def strip_table_labels(html, specs):
    """Take the template's table captions out of the note's wording.

    The signed set's text was read with its tables flattened, so a note's
    sentence still carries the rows of the table beside it: "... due to the
    following factors: Profit before income tax Tax calculated at a tax rate
    of 17% Tax effects of: - Statutory stepped income exemption Total income
    tax expenses for the financial year". The table is drawn as a table now,
    so those captions go from the sentence. Only a RUN of two or more captions
    is removed: one caption on its own may be an ordinary word of the note
    ("Trade receivables are non-interest bearing ...").
    """
    if not html:
        return html
    seen, labels = set(), []
    for spec in specs or []:
        if spec.get("source") != "template" or spec.get("note") in seen:
            continue
        seen.add(spec.get("note"))
        for table in read(spec.get("path")).get(str(spec.get("note")), []):
            for row in table["rows"]:
                text = re.sub(r"^[\-\u2013\u2022]\s*", "", row["label"] or "").strip()
                if len(text) >= 4:
                    labels.append(text)
    if len(labels) < 2:
        return html

    def one(label):
        label = label.replace("’", "'")
        parts = [re.escape(word) for word in label.split()]
        return r"\s+".join(parts).replace("'", "(?:['’]|&#39;|&#x27;|&rsquo;)")

    variants = set(labels)
    for label in labels:
        bare = re.sub(r"\s*\([^)]*\)\s*$", "", label).strip()   # "... 17% (2022: 17%)"
        if len(bare) >= 4:
            variants.add(bare)
    alternation = "|".join(one(l) for l in sorted(variants, key=len, reverse=True))
    caption = "(?:" + alternation + r")(?:\s*\[update:[^\]]*\])?"
    run = re.compile(r"(?:^|(?<=[\s>]))" + caption
                     + r"(?:\s*[\-–]?\s*" + caption + r")+", re.IGNORECASE)
    cleaned = run.sub("", html)
    # a single caption trailing the colon that introduces the table
    trailing = re.compile(r"(:)\s+(?:" + alternation + r")(\s*</p>)", re.IGNORECASE)
    cleaned = trailing.sub(r"\1\2", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([.:;,])", r"\1", cleaned)
    cleaned = re.sub(r"<p>\s+", "<p>", cleaned)
    cleaned = re.sub(r"\s+</p>", "</p>", cleaned)
    cleaned = re.sub(r"<p>\s*</p>", "", cleaned)
    return cleaned
