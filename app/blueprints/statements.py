"""Financial statement generation, editing, preview, versioning and sharing."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template,
                   request, send_file, url_for)
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (STATEMENT_TYPES, FinancialStatement, FinancialYear,
                      StatementLine, StatementVersion)
from ..services import statements as statement_service
from ..services import storage
from ..services import versions as version_service
from ..services import email as email_service
from ..services.audit import record
from ..services.compute import balance_check
from ..services.mapping import learn_mapping

bp = Blueprint("statements", __name__, url_prefix="/statements")


@bp.route("/fy/<int:fy_id>")
@login_required
def index(fy_id):
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    existing = {s.statement_type: s for s in financial_year.statements}

    verified_docs = sum(1 for d in financial_year.documents
                        if d.review_status == "verified")

    return render_template("statements/index.html",
                           fy=financial_year,
                           customer=financial_year.customer,
                           statement_types=STATEMENT_TYPES,
                           existing=existing,
                           verified_docs=verified_docs)


@bp.route("/fy/<int:fy_id>/generate", methods=["POST"])
@login_required
def generate(fy_id):
    """Build (or rebuild) every statement from the verified documents."""
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    # Statements exist only as a consequence of an approved trial balance.
    # Generating them any other way would let the two disagree.
    if not financial_year.tb_is_approved:
        flash("Approve the trial balance first — that is what generates the "
              "statements.", "error")
        return redirect(url_for("trial_balance.index", fy_id=fy_id))

    results = statement_service.build_all(fy_id)

    total_lines = sum(r.get("lines", 0) for r in results.values() if r.get("ok"))
    unmapped = sum(r.get("unmapped", 0) for r in results.values() if r.get("ok"))

    record("financial_year", fy_id, "generate_statements",
           after={"lines": total_lines, "unmapped": unmapped}, commit=True)

    message = f"Generated {len(results)} statements ({total_lines} lines)."
    if unmapped:
        message += (f" {unmapped} account(s) could not be mapped automatically "
                    f"— see the Unmapped items panel.")
        flash(message, "warning")
    else:
        flash(message, "success")

    return redirect(url_for("statements.index", fy_id=fy_id))


@bp.route("/<int:statement_id>")
@login_required
def detail(statement_id):
    statement = db.session.get(FinancialStatement, statement_id) or abort(404)
    financial_year = statement.financial_year

    check = None
    if statement.statement_type == "balance_sheet":
        check = balance_check(statement.lines)

    # Group lines for display headings.
    groups, seen = [], set()
    for line in statement.lines:
        if line.group_key not in seen:
            seen.add(line.group_key)
            groups.append(line.group_key)

    unmapped = _unmapped_for(financial_year, statement.statement_type)

    return render_template("statements/detail.html",
                           statement=statement, fy=financial_year,
                           customer=financial_year.customer,
                           groups=groups, check=check, unmapped=unmapped,
                           valid_keys=statement_service.line_keys_for(
                               statement.statement_type))


def _unmapped_for(financial_year, statement_type):
    """Extracted rows that didn't map onto any statement line."""
    from ..services.mapping import match_label
    from ..services.statements import _verified_items, line_keys_for

    valid = set(line_keys_for(statement_type))
    if not valid:
        return []

    unmapped = []
    for item in _verified_items(financial_year.id, statement_type):
        rule = match_label(item.label, financial_year.customer_id, statement_type)
        if rule and rule["line_key"] in valid:
            continue
        # Belongs to another statement rather than being unrecognised.
        if match_label(item.label, financial_year.customer_id) is not None:
            continue
        unmapped.append(item)
    return unmapped[:40]


@bp.route("/<int:statement_id>/preview")
@login_required
def preview(statement_id):
    """Print-styled view. The PDF renderer reuses this exact template."""
    statement = db.session.get(FinancialStatement, statement_id) or abort(404)
    financial_year = statement.financial_year

    groups, seen = [], set()
    for line in statement.lines:
        if line.group_key not in seen:
            seen.add(line.group_key)
            groups.append(line.group_key)

    return render_template("statements/preview.html",
                           statement=statement, fy=financial_year,
                           customer=financial_year.customer, groups=groups)


@bp.route("/api/line/<int:line_id>", methods=["PATCH"])
@login_required
def update_line(line_id):
    """Override one figure. The auto-calculated value is kept underneath."""
    line = db.session.get(StatementLine, line_id) or abort(404)
    payload = request.get_json(silent=True) or {}

    raw = payload.get("amount")
    before = str(line.effective_amount)

    if raw is None or str(raw).strip() == "":
        line.manual_override_amount = None          # revert to calculated
        line.source = "computed" if line.formula else "auto"
    else:
        try:
            line.manual_override_amount = Decimal(str(raw).replace(",", "").strip())
            line.source = "manual"
        except (InvalidOperation, ValueError):
            return jsonify({"ok": False, "error": "Not a valid number"}), 400

    record("statement_line", line.id, "override",
           before={"amount": before},
           after={"amount": str(line.effective_amount)})
    db.session.commit()

    # Totals depend on this line, so recompute the whole statement.
    statement_service.recalculate(line.statement_id)

    statement = db.session.get(FinancialStatement, line.statement_id)
    return jsonify({
        "ok": True,
        "overridden": line.is_overridden,
        "lines": [{"id": l.id, "amount": float(l.effective_amount or 0),
                   "overridden": l.is_overridden} for l in statement.lines],
    })


@bp.route("/api/map", methods=["POST"])
@login_required
def map_account():
    """Assign an unmapped account to a statement line, and remember it."""
    payload = request.get_json(silent=True) or {}
    label = (payload.get("label") or "").strip()
    line_key = (payload.get("line_key") or "").strip()
    statement_id = payload.get("statement_id")

    statement = db.session.get(FinancialStatement, statement_id) or abort(404)

    if not label or not line_key:
        return jsonify({"ok": False, "error": "label and line_key required"}), 400

    learn_mapping(statement.financial_year.customer_id, label,
                  statement.statement_type, line_key, current_user.id)

    record("account_mapping", None, "learn",
           after={"label": label, "line_key": line_key})
    db.session.commit()

    # Rebuild EVERY statement, not just this one. Mapping an account changes
    # a figure that flows downstream - a newly mapped expense moves the year's
    # profit, which moves retained earnings on the balance sheet and the
    # opening equity in the changes-in-equity statement. Rebuilding only the
    # edited statement leaves the others stale, and the balance check then
    # reports a difference that no longer exists.
    statement_service.build_all(statement.financial_year_id)

    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Version history and the customer email round-trip
# --------------------------------------------------------------------------

@bp.route("/fy/<int:fy_id>/versions")
@login_required
def versions(fy_id):
    """Every round of the review: what we sent, what came back, what's agreed."""
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    diffs = []
    all_versions = financial_year.versions          # newest first
    if len(all_versions) >= 2:
        diffs = version_service.compare(all_versions[1], all_versions[0])

    preview = None
    if financial_year.statements:
        latest = financial_year.latest_version
        if latest:
            preview = email_service.compose_statements_email(
                financial_year, latest)

    return render_template("statements/versions.html",
                           fy=financial_year,
                           customer=financial_year.customer,
                           versions=all_versions,
                           diffs=diffs,
                           preview=preview,
                           check=_balance_state(financial_year),
                           email_ready=email_service.email_enabled())


def _balance_state(financial_year):
    """Does the balance sheet currently balance?

    Checked before a version can be marked final: a customer's revision can
    easily be one-sided (raising an asset with no matching entry), and an
    unbalanced set must never become the basis of an audit report.
    """
    statement = next((s for s in financial_year.statements
                      if s.statement_type == "balance_sheet"), None)
    if statement is None:
        return None
    return balance_check(statement.lines)


@bp.route("/fy/<int:fy_id>/send", methods=["POST"])
@login_required
def send_to_customer(fy_id):
    """Snapshot the statements and email them to the customer for review."""
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    if not financial_year.statements:
        flash("Generate the statements before sending them.", "error")
        return redirect(url_for("statements.index", fy_id=fy_id))

    if not financial_year.customer.email:
        flash("This customer has no email address. Add one on the customer "
              "record first.", "error")
        return redirect(url_for("customers.edit",
                                customer_id=financial_year.customer_id))

    version = version_service.create_version(
        financial_year, source="auditor", status="draft",
        user_id=current_user.id)

    built = version_service.build_attachments(financial_year, version)
    if not built.get("ok"):
        flash(built.get("error", "Could not build the workbook."), "error")
        return redirect(url_for("statements.versions", fy_id=fy_id))

    message = email_service.compose_statements_email(
        financial_year, version, attachments=[version.xlsx_path])

    now = datetime.utcnow()
    for statement in financial_year.statements:
        statement.status = "shared"
        statement.shared_at = now
    financial_year.status = "statements_shared"
    financial_year.shared_at = now

    if email_service.email_enabled():
        result = email_service.send_email(
            message["to"], message["subject"], message["body"],
            attachments=[version.xlsx_path])

        if result["ok"]:
            version.status = "sent"
            version.sent_at = now
            version.sent_to = message["to"]
            record("statement_version", version.id, "send_email",
                   after={"to": message["to"], "version": version.version_no})
            db.session.commit()
            flash(f"Version {version.version_no} emailed to {message['to']}. "
                  f"Their reply will be picked up automatically.", "success")
        else:
            db.session.commit()
            flash(f"Could not send: {result['error']} The workbook is ready to "
                  f"download and send manually.", "error")
    else:
        db.session.commit()
        flash(f"Version {version.version_no} prepared. Email isn't configured "
              f"yet, so download the workbook below and send it yourself — or "
              f"add SMTP_USER and SMTP_PASSWORD to .env to send automatically.",
              "warning")

    return redirect(url_for("statements.versions", fy_id=fy_id))


@bp.route("/fy/<int:fy_id>/check-replies", methods=["POST"])
@login_required
def check_replies(fy_id):
    """Poll the mailbox for customer replies to this engagement."""
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    if not email_service.email_enabled():
        flash("Email isn't configured. Add SMTP_USER and SMTP_PASSWORD to "
              ".env, or use 'Upload customer's revised version' instead.",
              "warning")
        return redirect(url_for("statements.versions", fy_id=fy_id))

    token = email_service.build_token(financial_year)
    replies = email_service.fetch_replies(known_tokens={token})

    if not replies:
        flash("No new replies found in the mailbox.", "warning")
        return redirect(url_for("statements.versions", fy_id=fy_id))

    ingested = 0
    for reply in replies:
        workbook = next((a for a in reply["attachments"]
                         if a["filename"].lower().endswith((".xlsx", ".xls"))), None)

        if workbook is None:
            # A reply with no attachment is still worth keeping - often it
            # just says "these look fine".
            version = version_service.create_version(
                financial_year, source="customer", status="customer_revised",
                user_id=current_user.id, notes=reply["body"][:4000])
            version.received_at = reply["received_at"]
            version.received_from = reply["from_address"]
            db.session.commit()
            ingested += 1
            continue

        directory = version_service.versions_dir(financial_year)
        path = directory / f"reply_{reply['received_at']:%Y%m%d_%H%M%S}_{workbook['filename']}"
        path.write_bytes(workbook["content"])

        outcome = version_service.ingest_reply(
            financial_year, path, from_address=reply["from_address"],
            body=reply["body"], user_id=current_user.id)

        if outcome.get("ok"):
            ingested += 1
            flash(f"Reply from {reply['from_address']}: "
                  f"{outcome['changes']} figure(s) revised, "
                  f"{outcome['comments']} comment(s).", "success")
        else:
            flash(f"Could not read the attachment: {outcome.get('error')}",
                  "error")

    if ingested:
        record("financial_year", fy_id, "ingest_replies",
               after={"count": ingested}, commit=True)

    return redirect(url_for("statements.versions", fy_id=fy_id))


@bp.route("/fy/<int:fy_id>/upload-revision", methods=["POST"])
@login_required
def upload_revision(fy_id):
    """Manual fallback: the auditor uploads whatever the customer sent back."""
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    uploaded = request.files.get("revision")
    if not uploaded or not uploaded.filename:
        flash("Choose the customer's revised workbook to upload.", "error")
        return redirect(url_for("statements.versions", fy_id=fy_id))

    if not uploaded.filename.lower().endswith((".xlsx", ".xls")):
        flash("Upload the Excel workbook the customer edited (.xlsx).", "error")
        return redirect(url_for("statements.versions", fy_id=fy_id))

    directory = version_service.versions_dir(financial_year)
    path = directory / f"revision_{datetime.utcnow():%Y%m%d_%H%M%S}_{uploaded.filename}"
    uploaded.save(path)

    outcome = version_service.ingest_reply(
        financial_year, path,
        from_address=financial_year.customer.email,
        body=(request.form.get("note") or "").strip() or None,
        user_id=current_user.id)

    if not outcome.get("ok"):
        flash(outcome.get("error", "Could not read that workbook."), "error")
    else:
        flash(f"Saved as version {outcome['version'].version_no}: "
              f"{outcome['changes']} figure(s) revised, "
              f"{outcome['comments']} comment(s). Review the changes below.",
              "success")

    return redirect(url_for("statements.versions", fy_id=fy_id))


@bp.route("/version/<int:version_id>/download")
@login_required
def download_version(version_id):
    """Download the workbook for a version (sent, or the customer's reply)."""
    version = db.session.get(StatementVersion, version_id) or abort(404)

    which = request.args.get("which", "sent")
    path = version.revised_file_path if which == "revised" else version.xlsx_path

    if not path:
        abort(404)

    try:
        resolved = storage.assert_within_storage(path)
    except ValueError:
        abort(403)

    if not Path(resolved).exists():
        abort(404)

    return send_file(resolved, as_attachment=True,
                     download_name=Path(resolved).name)


@bp.route("/fy/<int:fy_id>/finalise", methods=["POST"])
@login_required
def finalise(fy_id):
    """Mark a version final. This is what unlocks the audit report."""
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    version_id = request.form.get("version_id", type=int)
    approved_by = (request.form.get("approved_by_name") or "").strip()

    version = (db.session.get(StatementVersion, version_id) if version_id
               else financial_year.latest_version)

    if version is None:
        flash("There is no version to finalise. Send the statements to the "
              "customer first.", "error")
        return redirect(url_for("statements.versions", fy_id=fy_id))

    if not approved_by:
        flash("Enter who at the customer agreed these figures.", "error")
        return redirect(url_for("statements.versions", fy_id=fy_id))

    # An unbalanced set must not silently become the basis of an audit report.
    # A customer's revision is easily one-sided - raising an asset with no
    # matching entry - so this is caught here rather than discovered in the
    # finished report.
    check = _balance_state(financial_year)
    if check and not check["balanced"] and not request.form.get("confirm_imbalance"):
        flash(f"The balance sheet does not balance — total assets differ from "
              f"total equity and liabilities by {check['difference']:,.2f}. "
              f"Fix the figures, or tick the confirmation box to finalise "
              f"anyway.", "error")
        return redirect(url_for("statements.versions", fy_id=fy_id))

    financial_year.approval_note = (request.form.get("approval_note")
                                    or "").strip() or None
    version_service.mark_final(financial_year, version,
                               approved_by=approved_by,
                               user_id=current_user.id)

    flash(f"Version {version.version_no} marked final, agreed by "
          f"{approved_by}. The audit report is now unlocked.", "success")
    return redirect(url_for("reports.builder", fy_id=fy_id))
