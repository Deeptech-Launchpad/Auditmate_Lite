"""The statement of cash flows as the preparer enters it.

Standard statement lines v8: "No plug line. The statement is held incomplete
until the preparer has entered it and closing cash agrees to the balance sheet
(PC-13)". The engine still works out what the two trial balances imply, but
only as a suggestion beside each box; what is printed is what was entered.
Subtotals are entered as stated too - the form fills them in as the lines
above are typed, and the preparer may overtype one.
"""
import re
from decimal import Decimal, InvalidOperation

from ..extensions import db
from ..models import AuditReport, CashFlowEntry

ZERO = Decimal("0")
OPENING = re.compile(r"(?i)^cash and cash equivalents at beginning")
NET = re.compile(r"(?i)^net cash flows|^net (increase|decrease|change)")


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def assign_keys(rows):
    """Give every figure row its key, in order. Repeated captions get #2, #3."""
    seen = {}
    for row in rows:
        if row.get("kind") not in ("item", "sub", "total"):
            continue
        base = _norm(row["label"])
        count = seen.get(base, 0)
        seen[base] = count + 1
        row["key"] = base if not count else f"{base}#{count + 1}"
    return rows


def saved(financial_year):
    try:
        return {e.row_key: Decimal(str(e.amount)) for e in
                CashFlowEntry.query.filter_by(financial_year_id=financial_year.id)}
    except Exception:                    # noqa: BLE001 - table not created yet
        db.session.rollback()
        return {}


def is_entered(financial_year):
    """Whether the preparer has saved this statement. False, not an error,
    before `flask init-db` has created the table."""
    try:
        return CashFlowEntry.query.filter_by(
            financial_year_id=financial_year.id).first() is not None
    except Exception:                    # noqa: BLE001
        db.session.rollback()
        return False


def wording_for(financial_year):
    """The cash flow section's template wording (its rows), from the report."""
    report = (AuditReport.query.filter_by(financial_year_id=financial_year.id)
              .order_by(AuditReport.id.desc()).first())
    if report is None:
        return None
    for section in report.sections:
        binding = section.data_binding or {}
        if binding.get("statement_type") == "cash_flow" and binding.get("cash_flow_wording"):
            return binding["cash_flow_wording"]
    return None


def form_rows(financial_year):
    """Every row of the statement, nil ones too, each with the engine's
    suggestion, or None when the cash flow is not built from a template."""
    from . import template_statements

    wording = wording_for(financial_year)
    if not wording or not wording.get("rows"):
        return None
    drawn = template_statements.cash_flow_layout(financial_year, wording, entry=True)
    return (drawn or {}).get("rows")


def parse_amount(text):
    text = (text or "").strip().replace(",", "").replace("S$", "").replace("$", "")
    if not text or text in ("-", "—", "–"):
        return ZERO
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(text)
    return -value if negative else value


def save(financial_year, form, user_id):
    """Store every posted figure. Saving is confirming the whole statement."""
    rows = form_rows(financial_year) or []
    posted = 0
    for row in rows:
        key = row.get("key")
        if not key:
            continue
        raw = form.get("cf_" + key)
        if raw is None:
            continue
        amount = parse_amount(raw)
        entry = CashFlowEntry.query.filter_by(
            financial_year_id=financial_year.id, row_key=key).first()
        if entry is None:
            entry = CashFlowEntry(financial_year_id=financial_year.id, row_key=key)
            db.session.add(entry)
        entry.label = row["label"][:255]
        entry.amount = amount
        entry.entered_by = user_id
        posted += 1
    db.session.commit()
    return posted


def clear(financial_year):
    CashFlowEntry.query.filter_by(financial_year_id=financial_year.id).delete()
    db.session.commit()


def overlay(financial_year, rows):
    """This year's column from the entries. Rows without an entry are nil."""
    entries = saved(financial_year)
    for row in rows:
        key = row.get("key")
        if key and row.get("kind") in ("item", "sub", "total"):
            row["cells"] = (entries.get(key, ZERO), row["cells"][1])
    return rows


def closing_cash(financial_year):
    from .bindings import figures_for

    value = figures_for(financial_year).resolve("BS-CASH", 0)
    return value if isinstance(value, Decimal) else ZERO


def check(financial_year):
    """Reasons the entered statement cannot stand, as plain sentences.
    None when the cash flow is not one the preparer enters."""
    rows = form_rows(financial_year)
    if rows is None:
        return None
    if not is_entered(financial_year):
        return ["The statement of cash flows has not been entered. Open "
                "Cash flow, check each line against your working, and save."]
    entries = saved(financial_year)
    opening = net = None
    for row in rows:
        key = row.get("key")
        if not key or key not in entries:
            continue
        if OPENING.match(row["label"]):
            opening = entries[key]
        elif row["kind"] == "sub" and NET.match(row["label"]):
            net = entries[key]
    closing_entered = next((entries[r["key"]] for r in rows
                            if r.get("kind") == "total" and r.get("key") in entries), None)
    close = closing_cash(financial_year)
    reasons = []
    if opening is not None and net is not None and abs(opening + net - close) >= 1:
        reasons.append(
            "Cash flow does not agree to the balance sheet: opening cash %s plus the "
            "net movement %s is %s, but cash on the balance sheet is %s."
            % (_fmt(opening), _fmt(net), _fmt(opening + net), _fmt(close)))
    elif closing_entered is not None and abs(closing_entered - close) >= 1:
        reasons.append("Closing cash entered (%s) differs from the balance sheet (%s)."
                       % (_fmt(closing_entered), _fmt(close)))
    return reasons


def _fmt(value):
    text = "{:,.2f}".format(abs(value))
    return "(%s)" % text if value < 0 else text
