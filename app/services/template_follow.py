"""Follow the template: only the disclosures last year's accounts had.

The library asks for more than a small company's signed accounts ever carried:
an ageing of receivables, a liquidity maturity table, a fair value hierarchy, a
covenant table. Each needs a document nobody has (or last year's version of
it), and its last-year column has nothing behind it, because last year's
accounts never printed it. The report then shows dozens of Incomplete cells
for disclosures the client has never made.

With this switched on (the default once a template is uploaded) a table the
library adds is LEFT OUT when both are true:

  * it depends on a document or a person rather than the books - its rows are
    bound to the aged listing, the loan schedule, the tax computation, a
    preparer's entry - and
  * the template's own note on that subject printed no figures.

A table drawn from the trial balance is never left out; it costs nothing to
print and last year can be split from it. Nothing is dropped silently: each
table left out is listed, with a button to put it back, because whether an
omission is acceptable is the preparer's call, not the software's.
"""
import logging
from pathlib import Path

log = logging.getLogger(__name__)

FOLLOW_TOKEN = "FOLLOW"        # off switch, per engagement
ADDBACK_TOKEN = "ADDBACK"      # a table put back, by its table id

# Bindings that name a document or a person rather than a line of the books.
_DOCUMENT_LIKE = {"AGED", "AGEDP", "TAX", "LOAN", "FAR", "REG", "KMP", "GL",
                  "PRIOR", "PRIORFS", "MANUAL", "MEMO", "CLIENT", "FIRM",
                  "BANK", "CF-MANUAL"}

_cache = {}


def _template_path(financial_year):
    from . import signed_notes

    customer = financial_year.customer
    path = getattr(customer, "report_template_path", None)
    if path and Path(str(path)).suffix.lower() == ".pdf" and Path(str(path)).exists():
        return Path(str(path))
    signed = signed_notes._signed_document(financial_year)
    if signed is not None and Path(signed.storage_path).exists():
        return Path(signed.storage_path)
    return None


def _figure_notes(financial_year):
    """{template note number: number of rows that print an amount}."""
    from . import signed_notes

    path = _template_path(financial_year)
    if path is None or path.suffix.lower() != ".pdf":
        return None
    key = (str(path), path.stat().st_mtime)
    if key not in _cache:
        counts = {}
        try:
            for row in signed_notes.read_rows(path):
                if row.get("note") and row.get("current") is not None:
                    counts[str(row["note"])] = counts.get(str(row["note"]), 0) + 1
        except Exception:                                    # noqa: BLE001
            log.exception("Could not read the template's note tables")
            return None
        _cache[key] = counts
    return _cache[key]


def _answer(financial_year, token, field):
    from . import document_fields

    row = document_fields.value(financial_year, token, field, "")
    return None if row is None else (row.text or "").strip().lower()


def enabled(financial_year):
    """Whether this engagement follows its template's disclosures."""
    if _template_path(financial_year) is None:
        return False
    return _answer(financial_year, FOLLOW_TOKEN, "template") != "off"


def _document_dependent(library_rows):
    figure = [r for r in library_rows
              if (r.get("binding") or "").strip() not in ("", "STATIC", "DOC:total")
              and not (r.get("binding") or "").startswith("BALANCE:")]
    if not figure:
        return False
    documents = sum(1 for r in figure
                    if (r.get("binding") or "").split(":", 1)[0].strip()
                    in _DOCUMENT_LIKE)
    return documents * 2 >= len(figure)


def _book_balance(table, financial_year):
    """Whether any row drawn from the books carries a balance this year."""
    from decimal import Decimal

    from .bindings import figures_for

    figures = figures_for(financial_year)
    for row in table.get("rows") or []:
        binding = (row.get("binding") or "").strip()
        if (not binding or binding in ("STATIC", "DOC:total")
                or binding.split(":", 1)[0].strip() in _DOCUMENT_LIKE):
            continue
        try:
            value = figures.resolve(binding, 0, table.get("table_id") or "")
        except Exception:                                    # noqa: BLE001
            continue
        if isinstance(value, Decimal) and value:
            return True
    return False


def _template_note_number(financial_year, note_code):
    """The template's number for the note this library note corresponds to."""
    from ..models import PriorYearNote
    from .reports import load_notes_catalogue

    for note in load_notes_catalogue(financial_year):
        if note.get("library_code") == note_code:
            key = note.get("key")
            prior = (PriorYearNote.query
                     .filter_by(financial_year_id=financial_year.id,
                                matched_key=key).first())
            return str(prior.note_number) if prior and prior.note_number else None
    return None


def decide(table, spec, financial_year):
    """(keep, reason). `reason` says why a table is left out."""
    if not enabled(financial_year):
        return True, ""
    table_id = table.get("table_id") or spec.get("table_id") or ""
    if _answer(financial_year, ADDBACK_TOKEN, table_id) == "yes":
        return True, ""
    # The statutory documents (directors' statement and the rest) are not
    # numbered notes and are never left out.
    if str(spec.get("note_code") or "").upper().startswith("S0") or str(
            table.get("table_id") or "").upper().startswith("S0"):
        return True, ""
    if not _document_dependent(table.get("rows") or []):
        return True, ""
    # A table that already carries a real balance from the books is content,
    # not a request for a document: related party balances, say.
    if _book_balance(table, financial_year):
        return True, ""

    counts = _figure_notes(financial_year)
    if counts is None:
        return True, ""                        # cannot read the template: keep
    number = _template_note_number(financial_year, spec.get("note_code"))
    if number and counts.get(number):
        return True, ""                        # the template printed figures here
    where = (f"its note {number} printed no figures" if number
             else "it had no note on this subject")
    return False, f"Last year's accounts: {where}"


def keeps(table, spec, financial_year):
    return decide(table, spec, financial_year)[0]


def left_out(report):
    """The tables this engagement leaves out, for the builder's panel."""
    from .bindings import library_table

    financial_year = report.financial_year
    if not enabled(financial_year):
        return []
    out, seen = [], set()
    for section in report.sections:
        if not section.is_enabled:
            continue
        for spec in (section.data_binding or {}).get("note_table_specs") or []:
            table = library_table(spec.get("version_id"), spec.get("table_id")) or {}
            if not table.get("rows"):
                continue
            keep, why = decide(table, spec, financial_year)
            tid = table.get("table_id") or spec.get("table_id")
            if keep or tid in seen:
                continue
            seen.add(tid)
            out.append({"table_id": tid, "note": section.title,
                        "heading": spec.get("heading") or table.get("heading") or "",
                        "rows": [r.get("label") for r in table["rows"]
                                 if r.get("label")][:5],
                        "why": why})
    return out


def panel(report):
    """What the builder shows: is the switch available, on, and what it left out."""
    financial_year = report.financial_year
    available = _template_path(financial_year) is not None
    return {"available": available, "on": enabled(financial_year),
            "left_out": left_out(report)}
