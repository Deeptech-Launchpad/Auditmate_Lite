"""Audit report assembly and PDF export.

Sections come from config/report_sections.yaml, so swapping in the real
24-section template is a config change rather than a code change.

PDF export uses WeasyPrint when it's installed (Linux VPS). On Windows dev
machines WeasyPrint needs GTK libraries and is awkward to install, so the app
falls back to a print-optimised HTML page the user can save via Ctrl+P.
"""
import functools
import logging
import re
from datetime import date

import yaml
from flask import current_app

from ..extensions import db
from ..models import AuditReport, AuditReportSection, FinancialStatement
from . import notes as notes_service

log = logging.getLogger(__name__)


# Section captions on the face of a statement. The client's template groups
# the balance sheet under ASSETS / EQUITY AND LIABILITIES with a secondary
# caption beneath, so a "|" separates the major caption from the minor one.
# A statement type with no entry here prints no captions at all, which is how
# the Statement of Comprehensive Income is presented.
GROUP_HEADINGS = {
    "balance_sheet": {
        "non_current_assets": "ASSETS|Non-current assets",
        "current_assets": "Current assets",
        "equity": "EQUITY AND LIABILITIES|Capital and reserves",
        "non_current_liabilities": "Non-current liabilities",
        "current_liabilities": "Current liabilities",
    },
    "cash_flow": {
        "operating": "Cash flows from operating activities",
        "investing": "Cash flows from investing activities",
        "financing": "Cash flows from financing activities",
    },
    # The supplementary Detailed Profit and Loss Statement does caption its
    # blocks, unlike the statutory statement it expands.
    "profit_and_loss_detailed": {
        "cost_of_sales": "Less: Cost of sales",
        "operating_expenses": "Less: Operating expenses",
    },
}


@functools.lru_cache(maxsize=1)
def load_sections():
    path = current_app.config["CONFIG_DIR"] / "report_sections.yaml"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or []



@functools.lru_cache(maxsize=1)
def optional_line_keys():
    """Line keys that appear on the face only when they carry a balance.

    Read from config/statement_templates.yaml so the presentation rule lives
    beside the line it applies to.
    """
    path = current_app.config["CONFIG_DIR"] / "statement_templates.yaml"
    if not path.exists():
        return frozenset()
    with open(path, "r", encoding="utf-8") as handle:
        spec = yaml.safe_load(handle) or {}
    return frozenset(
        line["key"]
        for statement in spec.values() if isinstance(statement, dict)
        for line in (statement.get("lines") or []) if line.get("optional"))


def render_bindings(text: str, customer, financial_year,
                    chips: bool = False) -> str:
    """Substitute {{ binding }} placeholders in template section content.

    `chips=True` is for the in-place editor: each binding becomes an
    uneditable span that shows the real value but remembers which binding it
    came from. Without it, an auditor editing the rendered text would
    silently bake this year's company name into next year's template.

    A deliberately small, safe substitution — not Jinja — because this text is
    auditor-editable and must never be able to execute anything.
    """
    if not text:
        return ""

    values = {
        "customer.name": customer.name or "",
        "customer.legal_name": customer.legal_name or customer.name or "",
        "customer.uen": customer.uen or "—",
        "customer.address": ", ".join(filter(None, [
            customer.address_line1, customer.address_line2,
            f"Singapore {customer.postal_code}" if customer.postal_code else None,
        ])) or "—",
        "fy.year_label": financial_year.year_label or "",
        "fy.end_date": (financial_year.end_date.strftime("%d %B %Y")
                        if financial_year.end_date else ""),
        "fy.start_date": (financial_year.start_date.strftime("%d %B %Y")
                          if financial_year.start_date else ""),
        "customer.director": customer.directors or "",
        "customer.directors": customer.directors or "",
        "customer.secretary": customer.company_secretary or "",
        "customer.contact": customer.contact_person or "",
        "customer.phone": customer.phone or "",
        "customer.email": customer.email or "",
        "customer.currency": customer.books_currency or "SGD",
        "today": date.today().strftime("%d %B %Y"),
        "firm.name": "AltiusNXT Audit",
    }

    def replace(match):
        key = match.group(1).strip()

        if key not in values:
            # An unknown placeholder must never reach a client-facing report
            # looking like template code. Flag it so it is obvious in the
            # preview that something needs filling in.
            body, css = f"[{key} not set]", "missing-binding"
        elif not str(values[key]).strip():
            body, css = "[not provided]", "missing-binding"
        else:
            # Several directors are stored one per line; render them so.
            body, css = str(values[key]).replace(chr(10), "<br>"), ""

        if chips:
            classes = ("ph " + css).strip()
            return (f'<span class="{classes}" contenteditable="false" '
                    f'data-ph="{key}">{body}</span>')
        return f'<span class="{css}">{body}</span>' if css else body

    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", replace, text)


def ensure_report(financial_year) -> AuditReport:
    """Get or create the report for a financial year, seeding its sections."""
    report = AuditReport.query.filter_by(
        financial_year_id=financial_year.id).first()

    if report is not None:
        return report

    report = AuditReport(
        financial_year_id=financial_year.id,
        title=f"Financial Statements — {financial_year.year_label}",
    )
    db.session.add(report)
    db.session.flush()

    for order, spec in enumerate(load_sections()):
        db.session.add(AuditReportSection(
            report_id=report.id,
            section_key=spec["key"],
            title=spec.get("title", spec["key"]),
            section_type=spec.get("type", "free_text"),
            sort_order=order,
            is_enabled=bool(spec.get("default_enabled", False)),
            content_html=spec.get("content", ""),
            data_binding={"statement_type": spec["statement_type"]}
            if spec.get("statement_type") else None,
        ))

    db.session.commit()
    return report


@functools.lru_cache(maxsize=1)
def _spec_index():
    """config/report_sections.yaml keyed by section key.

    Looked up at render time rather than copied into the database when the
    report is seeded, so editing the YAML changes existing reports too.
    """
    return {spec["key"]: spec for spec in load_sections()}


def section_payload(section, customer, financial_year, chips: bool = False):
    """Build what a section needs to render: text, tables and/or a statement."""
    spec = _spec_index().get(section.section_key, {})

    payload = {
        "section": section,
        "statement": None,
        "html": "",
        "tables": [],
        # The Detailed Profit and Loss Statement shows the breakdown lines
        # that the face of the statutory statement summarises away.
        "detailed": bool(spec.get("detailed")),
        "footnote": spec.get("footnote"),
    }

    if section.section_type == "statement":
        statement_type = (section.data_binding or {}).get("statement_type")
        if statement_type:
            payload["statement"] = FinancialStatement.query.filter_by(
                financial_year_id=financial_year.id,
                statement_type=statement_type).first()
    else:
        payload["html"] = render_bindings(section.content_html or "",
                                          customer, financial_year,
                                          chips=chips)
        payload["tables"] = notes_service.build_tables(
            spec.get("note_table"), financial_year)
        apply_note_overrides(section, payload["tables"])

    return payload


def apply_note_overrides(section, tables):
    """Lay the auditor's edits over the computed note tables.

    Note tables are recomputed from the trial balance on every render, so an
    edit cannot live on the row. It lives in `report_figure_overrides`,
    addressed by position, and is put back here.

    An override is applied only when the row at that position still has the
    label it had when the edit was made. If the note has since been rebuilt
    with different accounts, the edit is shown as stale rather than dropped
    onto whichever figure happens to sit there now - silently moving an
    auditor's correction onto a different account is the one outcome worth
    engineering against.
    """
    from ..models import ReportFigureOverride

    if not tables:
        return

    overrides = ReportFigureOverride.query.filter_by(
        report_id=section.report_id, section_key=section.section_key).all()
    if not overrides:
        return

    by_position = {(o.table_index, o.row_index): o for o in overrides}

    for table_index, table in enumerate(tables):
        for row_index, row in enumerate(table.get("rows", [])):
            override = by_position.get((table_index, row_index))
            if override is None:
                continue

            row["override_id"] = override.id
            if not override.matches(row):
                # The row moved. Say so on the face of the report rather
                # than applying the figure to the wrong account.
                row["stale_override"] = override.anchor_label
                continue

            if override.label_override is not None:
                row["original_label"] = row.get("label")
                row["label"] = override.label_override
                row["label_overridden"] = True
            if override.amount_override is not None:
                row["computed_current"] = row.get("current")
                row["current"] = override.amount_override
                row["overridden"] = True


def weasyprint_available() -> bool:
    try:
        import weasyprint      # noqa: F401
        return True
    except Exception:          # noqa: BLE001  (import can fail on missing GTK)
        return False


def render_pdf(html: str, base_url: str = None) -> bytes:
    """Render assembled HTML into PDF bytes. Raises if WeasyPrint is absent."""
    import weasyprint
    return weasyprint.HTML(string=html, base_url=base_url).write_pdf()
