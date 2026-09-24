"""The share capital note: number of shares and amount, for each year.

The library's table (N33_SHARE_CAPITAL_T3) has four figure columns - the
number of shares and the amount, this year and last - which the ordinary
one-amount-per-year builder cannot lay out, so the note printed "several
columns per year, which is not laid out yet".

Where the figures come from:

  the amounts       the balance sheet: share capital this year, last year and
                    (from the signed set) the year before, which is last
                    year's opening
  last year's       the signed accounts' own share capital note, which prints
  share counts      a count and an amount for each of the two years it covers
  this year's count nothing in the books holds it. Carried from last year's
                    count where the amount did not move - shares here have no
                    par value, so an unchanged amount means none were issued.
                    Where the amount did move the count is asked for.

A person can still overwrite any count on the Figures page; what they typed
is what prints.
"""
import logging
import re
from decimal import Decimal
from pathlib import Path

log = logging.getLogger(__name__)

TOKEN = "PRIORSHARES"
ZERO = Decimal("0")

_FOUR = re.compile(r"(\d[\d,]*)\s+(\d[\d,]*)\s+(\d[\d,]*)\s+(\d[\d,]*)\s*$")
_HEAD = re.compile(r"^\d+\.\s*share capital\b", re.IGNORECASE)


def _number(text):
    return Decimal(text.replace(",", ""))


def read_share_capital(path):
    """(shares, amount, shares, amount): last year's, then the year before's.

    From the signed accounts' share capital note, which prints one row -
    "Beginning and end of financial year" - when nothing was issued.
    """
    import pdfplumber

    try:
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages[3:]:
                lines = (page.extract_text() or "").splitlines()
                for index, line in enumerate(lines):
                    if not _HEAD.match(line.strip()):
                        continue
                    for row in lines[index + 1:index + 9]:
                        match = _FOUR.search(row.strip())
                        if match:
                            return tuple(_number(g) for g in match.groups())
    except Exception:                                        # noqa: BLE001
        log.exception("Could not read the share capital note of %s", path)
    return None


def fill(financial_year):
    """Store last year's share count and amount from the signed note."""
    from ..extensions import db
    from ..models import DocumentFigure
    from . import signed_notes

    DocumentFigure.query.filter_by(financial_year_id=financial_year.id,
                                   token=TOKEN).delete()
    signed = signed_notes._signed_document(financial_year)
    if signed is None:
        return 0
    path = Path(signed.storage_path)
    if not path.exists() or path.suffix.lower() != ".pdf":
        return 0
    found = read_share_capital(path)
    if not found:
        return 0
    for field, amount in zip(("number_1", "amount_1", "number_2", "amount_2"),
                             found):
        db.session.add(DocumentFigure(
            financial_year_id=financial_year.id, token=TOKEN, field=field,
            scope="", member="", amount=amount,
            found_at="Signed accounts, share capital note"))
    db.session.flush()
    return 4


def _stored(financial_year, field):
    from ..models import DocumentFigure

    row = DocumentFigure.query.filter_by(
        financial_year_id=financial_year.id, token=TOKEN, field=field).first()
    return None if row is None or row.amount is None else Decimal(str(row.amount))


def _entered(financial_year, field):
    """A count a person typed for this year (REG:share_open and the rest)."""
    from . import document_fields

    row = document_fields.value(financial_year, "REG", field, "")
    return None if row is None or row.amount is None else Decimal(str(row.amount))


def is_share_capital_table(table):
    labels = str(table.get("column_labels") or "").strip().lower()
    return labels.startswith("number of shares")


def table(spec, financial_year, library_table, figures):
    """The four-column table, or None when there is no share capital at all."""
    from .bindings import Held, _is_held

    first_year = bool(financial_year.is_first_year)
    close_now = figures.resolve("BS-SC", 0)
    if not isinstance(close_now, Decimal) or not close_now:
        return None

    def amount(offset):
        value = figures.resolve("BS-SC", offset)
        return value if isinstance(value, Decimal) else None

    open_now = amount(1) if not first_year else ZERO
    close_prev = amount(1) if not first_year else None
    # The year before last year: the signed set's own note prints it.
    open_prev = _stored(financial_year, "amount_2")
    if open_prev is None and not first_year:
        open_prev = amount(2)

    need = ("Needs the share register (number of shares)")

    def held(reason, field):
        return Held(reason, token="REG", field=field, scope="")

    # Counts.
    count_prev_close = _stored(financial_year, "number_1")
    count_prev_open = _stored(financial_year, "number_2")
    count_open = _entered(financial_year, "share_open")
    if count_open is None:
        count_open = count_prev_close
    count_issued = _entered(financial_year, "share_issued")
    count_close = None
    if count_open is not None:
        if count_issued is not None:
            count_close = count_open + count_issued
        elif open_now is not None and open_now == close_now:
            count_issued, count_close = ZERO, count_open      # nothing issued
    if count_close is None:
        count_close = held(need, "share_close")
    if count_issued is None:
        count_issued = held(need, "share_issued")
    if count_open is None:
        count_open = held(need, "share_open")

    def diff(a, b):
        return (a - b) if isinstance(a, Decimal) and isinstance(b, Decimal) else None

    issued_now = diff(close_now, open_now)
    issued_prev = diff(close_prev, open_prev)
    count_issued_prev = (ZERO if issued_prev == ZERO and count_prev_open is not None
                         and count_prev_open == count_prev_close else None)

    def cell(value, why):
        return value if value is not None else Held(why)

    year_gap = "Last year's opening is not in the signed accounts"
    rows = [
        {"label": "Balance at the beginning of the financial year",
         "cells": [count_open, cell(open_now, year_gap),
                   None if first_year else cell(count_prev_open, year_gap),
                   None if first_year else cell(open_prev, year_gap)]},
        {"label": "Shares issued during the financial year",
         "cells": [count_issued, cell(issued_now, year_gap),
                   None if first_year else cell(count_issued_prev, year_gap),
                   None if first_year else cell(issued_prev, year_gap)]},
        {"label": "Balance at the end of the financial year",
         "cells": [count_close, close_now,
                   None if first_year else cell(count_prev_close, year_gap),
                   None if first_year else cell(close_prev, year_gap)],
         "bold": True, "rule": True},
    ]

    for row in rows:
        row.setdefault("bold", False)
        row.setdefault("rule", False)
        for index, value in enumerate(row["cells"]):
            if _is_held(value):
                row.setdefault("held", {})[index] = value.reason
                row.setdefault("held_edit", {})[index] = (
                    {"token": value.token, "field": value.field,
                     "scope": value.scope, "member": value.member}
                    if value.editable else None)
                row["cells"][index] = None

    columns = ["Number of shares", "Amount"]
    if not first_year:
        columns += ["Number of shares", "Amount"]
    if first_year:
        for row in rows:
            row["cells"] = row["cells"][:2]
            row["held"] = {i: w for i, w in (row.get("held") or {}).items() if i < 2}
            row["held_edit"] = {i: w for i, w in (row.get("held_edit") or {}).items()
                                if i < 2}

    return {"heading": spec.get("heading"),
            "table_id": library_table.get("table_id"),
            "by_class": True, "classes": columns, "rows": rows,
            "year_groups": True,
            "subheads": ["No.", "S$"] * (1 if first_year else 2),
            "columns": library_table.get("column_labels")}
