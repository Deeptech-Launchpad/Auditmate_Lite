"""Statement version history - the customer review round-trip.

Life of an engagement's statements:

    v1  draft            auditor generates the statements
    v1  sent             emailed to the customer (Excel + PDF)
    v2  customer_revised customer replies with edits -> new version
    v3  draft            auditor applies/adjusts, ready to send again
        ...              repeat until agreed
    vN  final            marked final -> unlocks the audit report

Each version stores a full JSON snapshot of the figures rather than pointing
at live rows, so an old version still shows what it showed at the time even
after the statements are rebuilt from source documents.
"""
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from flask import current_app

from ..extensions import db
from ..models import FinancialStatement, StatementVersion
from . import statements as statement_service
from .audit import record

log = logging.getLogger(__name__)


def _snapshot(financial_year) -> dict:
    """Capture every statement and line as plain JSON."""
    payload = {
        "captured_at": datetime.utcnow().isoformat(),
        "year_label": financial_year.year_label,
        "customer": financial_year.customer.name,
        "currency": financial_year.customer.books_currency,
        "statements": [],
    }

    for statement in sorted(financial_year.statements,
                            key=lambda s: s.statement_type):
        payload["statements"].append({
            "statement_type": statement.statement_type,
            "type_label": statement.type_label,
            "lines": [{
                "line_key": line.line_key,
                "label": line.label,
                "group_key": line.group_key,
                "indent": line.indent,
                "amount": float(line.effective_amount or 0),
                "amount_previous": (float(line.amount_previous)
                                    if line.amount_previous is not None else None),
                "is_subtotal": bool(line.is_subtotal),
                "is_total": bool(line.is_total),
                "overridden": bool(line.is_overridden),
            } for line in statement.lines],
        })

    return payload


def versions_dir(financial_year) -> Path:
    root = Path(current_app.config["STORAGE_ROOT"])
    path = (root / str(financial_year.customer_id) /
            str(financial_year.id) / "versions")
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_version(financial_year, source="auditor", status="draft",
                   user_id=None, notes=None) -> StatementVersion:
    """Snapshot the current statements as a new version."""
    from .email import build_token

    last = (StatementVersion.query
            .filter_by(financial_year_id=financial_year.id)
            .order_by(StatementVersion.version_no.desc()).first())
    next_no = (last.version_no + 1) if last else 1

    version = StatementVersion(
        financial_year_id=financial_year.id,
        version_no=next_no,
        source=source,
        status=status,
        token=build_token(financial_year),
        snapshot=_snapshot(financial_year),
        notes=notes,
        created_by=user_id,
    )
    db.session.add(version)
    db.session.flush()

    record("statement_version", version.id, "create",
           after={"version": next_no, "source": source})
    db.session.commit()

    log.info("Created version %s for FY %s", next_no, financial_year.id)
    return version


def build_attachments(financial_year, version) -> dict:
    """Write the Excel (and PDF if available) the customer will receive."""
    from .excel_io import build_workbook

    statements = sorted(financial_year.statements,
                        key=lambda s: s.statement_type)
    if not statements:
        return {"ok": False, "error": "No statements to send"}

    safe_name = "".join(c if c.isalnum() else "_"
                        for c in financial_year.customer.name)[:40]
    base = f"{safe_name}_{financial_year.year_label}_Statements_v{version.version_no}"
    directory = versions_dir(financial_year)

    xlsx_path = directory / f"{base}.xlsx"
    try:
        build_workbook(financial_year, statements, xlsx_path,
                       version.version_no, version.token)
        version.xlsx_path = str(xlsx_path)
    except Exception as exc:                       # noqa: BLE001
        log.exception("Workbook build failed")
        return {"ok": False, "error": f"Could not build the workbook: {exc}"}

    db.session.commit()
    return {"ok": True, "xlsx": str(xlsx_path)}


def apply_customer_changes(financial_year, version, changes, comments=None,
                           user_id=None):
    """Apply a customer's revised figures onto the live statements.

    Each change becomes a manual override on the matching statement line, so
    the auditor sees exactly which figures the customer moved and can revert
    any of them individually.
    """
    applied, skipped = [], []

    by_type = {s.statement_type: s for s in financial_year.statements}
    lines_by_key = {}
    for statement in by_type.values():
        for line in statement.lines:
            lines_by_key.setdefault(line.line_key, line)

    for change in changes:
        line = lines_by_key.get(change.get("line_key"))
        if line is None:
            skipped.append(change)
            continue

        before = float(line.effective_amount or 0)
        line.manual_override_amount = Decimal(str(change["revised"]))
        line.source = "manual"
        applied.append({**change, "before": before})

        record("statement_line", line.id, "customer_revision",
               before={"amount": before},
               after={"amount": change["revised"]})

    db.session.commit()

    # Totals depend on the lines that just moved.
    for statement in by_type.values():
        statement_service.recalculate(statement.id)

    version.snapshot = _snapshot(financial_year)
    if comments:
        version.customer_comments = "\n".join(
            f"{c['label']}: {c['comment']}" for c in comments)
    db.session.commit()

    return {"applied": applied, "skipped": skipped}


def ingest_reply(financial_year, file_path: Path, from_address=None,
                 body=None, user_id=None) -> dict:
    """Turn a customer's returned workbook into the next version.

    Used by both paths: the automatic Gmail pickup and the manual
    "upload customer's revised version" button.
    """
    from .excel_io import read_workbook

    parsed = read_workbook(Path(file_path))
    if parsed.get("error"):
        return {"ok": False, "error": parsed["error"]}

    version = create_version(financial_year, source="customer",
                             status="customer_revised", user_id=user_id)
    version.revised_file_path = str(file_path)
    version.received_at = datetime.utcnow()
    version.received_from = from_address
    if body:
        version.notes = body[:4000]
    db.session.commit()

    outcome = apply_customer_changes(
        financial_year, version, parsed["changes"],
        comments=parsed["comments"], user_id=user_id)

    financial_year.status = "statements_shared"
    db.session.commit()

    return {
        "ok": True,
        "version": version,
        "changes": len(outcome["applied"]),
        "skipped": len(outcome["skipped"]),
        "comments": len(parsed["comments"]),
    }


def mark_final(financial_year, version, approved_by=None, user_id=None):
    """Freeze a version as the agreed set. This unlocks the audit report."""
    for other in financial_year.versions:
        if other.status == "final":
            other.status = "customer_revised"

    version.status = "final"
    version.snapshot = _snapshot(financial_year)

    now = datetime.utcnow()
    for statement in financial_year.statements:
        statement.status = "approved"
        statement.approved_at = now

    financial_year.status = "approved"
    financial_year.approved_at = now
    if approved_by:
        financial_year.approved_by_name = approved_by

    record("statement_version", version.id, "mark_final",
           after={"version": version.version_no, "by": approved_by})
    db.session.commit()

    return version


def compare(version_a, version_b) -> list:
    """Line-by-line differences between two versions."""
    def flatten(version):
        out = {}
        for statement in (version.snapshot or {}).get("statements", []):
            for line in statement.get("lines", []):
                out[(statement["statement_type"], line["line_key"])] = line
        return out

    old, new = flatten(version_a), flatten(version_b)
    diffs = []

    for key in sorted(set(old) | set(new)):
        before = old.get(key, {}).get("amount")
        after = new.get(key, {}).get("amount")
        if before is None and after is None:
            continue
        if before == after:
            continue
        label = (new.get(key) or old.get(key) or {}).get("label", key[1])
        diffs.append({
            "statement": key[0],
            "line_key": key[1],
            "label": label,
            "before": before,
            "after": after,
            "delta": (after or 0) - (before or 0),
        })

    return diffs
