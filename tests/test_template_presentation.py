"""Prior-year layout for PL and BS, now genuinely a Phase 3 workstream (J).

J1: the profit and loss statement no longer sweeps every operating expense
into one guessed "Administrative expenses" row when the template never
offered that caption in the first place - it falls back to one line per
expense, the PL equivalent of the balance sheet's own extras path.

J2: the template's line order/headings/cash-flow wording are refreshed on
every statement rebuild, not just once at report creation - and that
refresh never touches anything a preparer owns (section on/off, wording
source, overrides, an adopted library version).
"""
from decimal import Decimal

from app.extensions import db
from app.models import StatementLine
from app.services import template_statements as ts

from . import factories as f


def _line(key, amount, previous=None, label=None):
    """A real StatementLine, unattached to any statement - every attribute
    _present_profit_and_loss() and the balance sheet's extras loop read off
    a line (effective_amount, effective_label, is_subtotal/is_total,
    formula) is a genuine column or property on the model, not a guess at
    its shape."""
    return StatementLine(
        line_key=key, label=label or key.replace("_", " ").title(),
        group_key="pl_flat", sort_order=0,
        amount_current=Decimal(str(amount)),
        amount_previous=Decimal(str(previous)) if previous is not None else None,
    )


# --- J1: the PL extras path -------------------------------------------------

def test_admin_expenses_still_swept_when_the_template_recognised_it(db):
    """Unchanged behaviour: a template that DOES single out administrative
    expenses as its own caption still gets one combined row - the sweep was
    never wrong for that case, only for the one below."""
    book = {
        "staff_salaries": _line("staff_salaries", 300000),
        "depreciation": _line("depreciation", 20000),
        "bank_charges": _line("bank_charges", 2000),
    }
    rows = [{"type": "admin_expenses", "label": "Administrative expenses"},
            {"type": "revenue", "label": "Revenue"},
            {"type": "profit_before_tax", "label": "Profit before tax"},
            {"type": "profit_for_year", "label": "Profit for the year"}]
    drawn = ts._present_profit_and_loss(book, rows)
    admin_rows = [d for d in drawn if d.line_key == "admin_expenses"]
    assert len(admin_rows) == 1
    assert admin_rows[0].amount_current == Decimal("-322000")
    individual = [d for d in drawn if d.line_key in
                 ("staff_salaries", "depreciation", "bank_charges")]
    assert individual == []


def test_expenses_shown_individually_when_the_template_never_recognised_admin_expenses(db):
    """J1: the actual fix. A template with no admin-expenses caption at all
    (because it lists its expenses out individually) no longer gets a
    guessed combined row - each expense prints under its own standard
    label instead, and nothing is lost or double-counted."""
    book = {
        "staff_salaries": _line("staff_salaries", 300000),
        "depreciation": _line("depreciation", 20000),
        "bank_charges": _line("bank_charges", 2000),
        "consulting_fee": _line("consulting_fee", 0),  # nil both years
    }
    rows = [{"type": "revenue", "label": "Revenue"},
            {"type": "profit_before_tax", "label": "Profit before tax"},
            {"type": "profit_for_year", "label": "Profit for the year"}]
    drawn = ts._present_profit_and_loss(book, rows)
    assert not any(d.line_key == "admin_expenses" for d in drawn)
    individual = {d.line_key: d.amount_current for d in drawn
                 if d.line_key in ("staff_salaries", "depreciation",
                                   "bank_charges", "consulting_fee")}
    assert individual == {"staff_salaries": Decimal("-300000"),
                          "depreciation": Decimal("-20000"),
                          "bank_charges": Decimal("-2000")}
    assert "consulting_fee" not in individual  # nil in both years, left off


# --- J2: the rebuild-safe presentation refresh ------------------------------

def test_apply_presentation_only_writes_presentation_headings_and_cash_flow(db):
    """The narrow slice `refresh_template_presentation()` calls on every
    rebuild must never touch anything else - is_enabled, content_html,
    wording_source, an adopted library version."""
    version = f.library_version()
    rep = f.report(note_keys=("revenue",))
    section = rep.sections[0]
    section.is_enabled = True
    section.content_html = "<p>Untouched.</p>"
    section.wording_source = "prior"
    section.library_version_adopted = version.id
    db.session.flush()

    before = (section.is_enabled, section.content_html,
             section.wording_source, section.library_version_adopted)

    profile = {"profit_and_loss": [{"type": "revenue"}]}
    # This section isn't a statement section, so nothing in the profile
    # matches its key - apply_presentation() must still leave it alone
    # rather than erroring.
    changed = ts.apply_presentation(rep, profile)
    assert changed == 0

    after = (section.is_enabled, section.content_html,
            section.wording_source, section.library_version_adopted)
    assert before == after
