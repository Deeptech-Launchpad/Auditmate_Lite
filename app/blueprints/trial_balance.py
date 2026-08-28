"""The Standard Trial Balance workspace.

This sits between Documents and Statements: every input merges here, the
auditor gets it balancing and fully mapped, and approving it is what releases
the financial statements and, in turn, the audit report.
"""
from flask import (Blueprint, abort, flash, jsonify, redirect,
                   render_template, request, session, url_for)
from flask_login import current_user, login_required

from ..extensions import db
from ..models import FinancialYear, TrialBalanceAccount
from ..services import mapping_review, outward, reconcile
from ..services import trial_balance as tb_service
from ..services.statements import line_keys_for, load_templates

bp = Blueprint("trial_balance", __name__, url_prefix="/trial-balance")


def _statement_line_options():
    """Every statement line an account can be mapped to, grouped for a picker."""
    options = []
    for statement_type, spec in load_templates().items():
        if statement_type == "trial_balance":
            continue
        lines = [l for l in (spec.get("lines") or [])
                 if not l.get("subtotal") and not l.get("total")]
        if lines:
            options.append({
                "statement": spec.get("title", statement_type),
                "lines": [{"key": l["key"], "label": l.get("label", l["key"])}
                          for l in lines],
            })
    return options


@bp.route("/fy/<int:fy_id>")
@login_required
def index(fy_id):
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    accounts = (TrialBalanceAccount.query
                .filter_by(financial_year_id=fy_id)
                .order_by(TrialBalanceAccount.account_code,
                          TrialBalanceAccount.account_name)
                .all())

    verified_docs = sum(1 for d in financial_year.documents
                        if d.review_status == "verified")

    sources, evidence = tb_service.choose_sources(financial_year.documents)

    return render_template("trial_balance/index.html",
                           fy=financial_year,
                           customer=financial_year.customer,
                           accounts=accounts,
                           totals=financial_year.tb_totals,
                           verified_docs=verified_docs,
                           sources=sources,
                           evidence=evidence,
                           checks=reconcile.check(financial_year),
                           outward=outward.check(financial_year),
                           mapping=mapping_review.review(financial_year),
                           line_options=_statement_line_options(),
                           # An empty Code column is noise; show it only when
                           # the client's chart of accounts actually uses one.
                           has_codes=any(a.account_code for a in accounts))


@bp.route("/fy/<int:fy_id>/mapping")
@login_required
def mapping(fy_id):
    """Every account against the statement line it maps to.

    Separate from the trial balance grid because it is a different job. The
    grid is about figures - do they balance. This is about meaning - what is
    each account, and the answer has to hold for years rather than for this
    engagement.
    """
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)
    return render_template("trial_balance/mapping.html",
                           fy=financial_year,
                           customer=financial_year.customer,
                           review=mapping_review.review(financial_year),
                           line_options=_statement_line_options())


@bp.route("/fy/<int:fy_id>/mapping/apply", methods=["POST"])
@login_required
def apply_mapping_suggestions(fy_id):
    """Accept every suggestion sitting against an unmapped account."""
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    if financial_year.tb_is_approved:
        flash("The trial balance is approved. Reopen it to change mappings.",
              "error")
        return redirect(url_for("trial_balance.mapping", fy_id=fy_id))

    applied = mapping_review.apply_suggestions(financial_year,
                                               user_id=current_user.id)
    if applied:
        flash(f"Mapped {applied} account(s) from the suggestions. Check them "
              f"- a suggestion is a starting point, not a decision.", "success")
    else:
        flash("Nothing to apply: every unmapped account needs a person.",
              "info")
    return redirect(url_for("trial_balance.mapping", fy_id=fy_id))


@bp.route("/fy/<int:fy_id>/build", methods=["POST"])
@login_required
def build(fy_id):
    """Merge every current source into the standard trial balance."""
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    verified = sum(1 for d in financial_year.documents
                   if d.review_status == "verified")
    if not verified:
        flash("No verified documents yet. Verify at least one document in "
              "Review & Correct before building the trial balance.", "error")
        return redirect(url_for("trial_balance.index", fy_id=fy_id))

    result = tb_service.build(fy_id, user_id=current_user.id)

    if not result.get("ok"):
        flash(result.get("error", "Could not build the trial balance."), "error")
        return redirect(url_for("trial_balance.index", fy_id=fy_id))

    message = (f"Trial balance built from "
               f"{', '.join(result['built_from'])}: {result['accounts']} "
               f"accounts, debits {result['debit']:,.2f} vs credits "
               f"{result['credit']:,.2f}.")
    if result["checked_against"]:
        message += (f" {len(result['checked_against'])} other document(s) "
                    f"kept back to check it against.")
    if not result["balanced"]:
        flash(message + f" Out by {result['difference']:,.2f} — check the "
                        f"source documents.", "warning")
    elif result["unmapped"]:
        flash(message + f" {result['unmapped']} account(s) still need mapping "
                        f"to a statement line.", "warning")
    else:
        flash(message + " Balanced and fully mapped.", "success")

    return redirect(url_for("trial_balance.index", fy_id=fy_id))


@bp.route("/fy/<int:fy_id>/add", methods=["POST"])
@login_required
def add(fy_id):
    """Add a missing account, or post an audit adjustment."""
    db.session.get(FinancialYear, fy_id) or abort(404)

    name = (request.form.get("account_name") or "").strip()
    if not name:
        flash("Enter an account name.", "error")
        return redirect(url_for("trial_balance.index", fy_id=fy_id))

    tb_service.add_account(
        fy_id, name,
        debit=request.form.get("debit") or None,
        credit=request.form.get("credit") or None,
        account_code=request.form.get("account_code"),
        standard_key=request.form.get("standard_key") or None,
        is_adjustment=bool(request.form.get("is_adjustment")),
        notes=(request.form.get("notes") or "").strip() or None,
        user_id=current_user.id)

    flash(f"Added “{name}” to the trial balance.", "success")
    return redirect(url_for("trial_balance.index", fy_id=fy_id))


@bp.route("/fy/<int:fy_id>/approve", methods=["POST"])
@login_required
def approve(fy_id):
    """Approve the trial balance and generate the statements from it."""
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    result = tb_service.approve(
        fy_id,
        approved_by=(request.form.get("approved_by") or "").strip() or None,
        user_id=current_user.id,
        force_unbalanced=bool(request.form.get("confirm_unbalanced")))

    if not result.get("ok"):
        flash(result.get("error", "Could not approve the trial balance."),
              "error")
        return redirect(url_for("trial_balance.index", fy_id=fy_id))

    built = sum(1 for r in result.get("statements", {}).values()
                if r.get("ok"))
    flash(f"Trial balance approved. {built} financial statements generated "
          f"from it.", "success")
    return redirect(url_for("statements.index", fy_id=fy_id))


@bp.route("/fy/<int:fy_id>/reopen", methods=["POST"])
@login_required
def reopen(fy_id):
    db.session.get(FinancialYear, fy_id) or abort(404)
    tb_service.reopen(fy_id, user_id=current_user.id)
    flash("Trial balance reopened for editing. Re-approve it to regenerate "
          "the statements.", "success")
    return redirect(url_for("trial_balance.index", fy_id=fy_id))


# --------------------------------------------------------------------------
# JSON API used by the grid
# --------------------------------------------------------------------------

@bp.route("/api/account/<int:account_id>", methods=["PATCH"])
@login_required
def update_account(account_id):
    account = db.session.get(TrialBalanceAccount, account_id) or abort(404)
    payload = request.get_json(silent=True) or {}

    if "standard_key" in payload:
        result = tb_service.set_mapping(account_id, payload["standard_key"],
                                        user_id=current_user.id)
        if not result.get("ok"):
            return jsonify(result), 400

    if "debit" in payload or "credit" in payload:
        result = tb_service.update_amounts(
            account_id, debit=payload.get("debit"),
            credit=payload.get("credit"), user_id=current_user.id)
        if not result.get("ok"):
            return jsonify(result), 400

    if "account_name" in payload:
        account.account_name = (payload["account_name"] or "").strip() \
            or account.account_name
        db.session.commit()

    totals = account.financial_year.tb_totals
    return jsonify({
        "ok": True,
        "mapped": account.is_mapped,
        # Category and FS follow from the mapping, so a remap has to send the
        # new values back - otherwise the grid would show the old heading
        # against the new line until the page was reloaded.
        "category": account.category_label,
        "fs": account.fs_label,
        "totals": {
            "debit": float(totals["debit"]),
            "credit": float(totals["credit"]),
            "difference": float(totals["difference"]),
            "balanced": totals["balanced"],
            "unmapped": totals["unmapped"],
        },
    })


@bp.route("/api/account/<int:account_id>", methods=["DELETE"])
@login_required
def delete_account(account_id):
    result = tb_service.delete_account(account_id, user_id=current_user.id)
    return jsonify(result), (200 if result.get("ok") else 404)


# --------------------------------------------------------------------------
# Customer review: send the link, then rule on what comes back
# --------------------------------------------------------------------------

@bp.route("/fy/<int:fy_id>/review")
@login_required
def review(fy_id):
    """Link management and the accept/reject screen."""
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    from ..services import email as email_service

    return render_template("trial_balance/review.html",
                           fy=financial_year,
                           customer=financial_year.customer,
                           totals=financial_year.tb_totals,
                           versions=financial_year.tb_versions,
                           link=financial_year.active_review_link,
                           email_ready=email_service.email_enabled())


@bp.route("/fy/<int:fy_id>/send-review", methods=["POST"])
@login_required
def send_review(fy_id):
    """Mint a link and email it to the customer."""
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    from ..services import email as email_service
    from ..services import tb_review

    if not financial_year.tb_accounts:
        flash("Build the trial balance before sending it for review.", "error")
        return redirect(url_for("trial_balance.index", fy_id=fy_id))

    if not financial_year.customer.email:
        flash("This customer has no email address. Add one first.", "error")
        return redirect(url_for("customers.edit",
                                customer_id=financial_year.customer_id))

    passcode = None
    if request.form.get("use_passcode"):
        import secrets
        passcode = f"{secrets.randbelow(10000):04d}"

    link, raw_token, passcode = tb_review.create_link(
        financial_year,
        expires_days=request.form.get("expires_days", 30, type=int),
        passcode=passcode,
        user_id=current_user.id)

    url = tb_review.review_url(raw_token)
    version = tb_review.create_version(financial_year, source="auditor",
                                       status="sent", link=link,
                                       user_id=current_user.id)
    version.sent_to = financial_year.customer.email

    message = tb_review.compose_email(financial_year, url, passcode)

    # The outcome travels with the link, because the panel that shows the
    # link has to say whether an email actually went out. A flash at the top
    # of a long page is not an answer to "did that send?".
    outcome = {"sent": False, "to": message["to"], "error": None}

    if email_service.email_enabled():
        result = email_service.send_email(message["to"], message["subject"],
                                          message["body"])
        if result["ok"]:
            from datetime import datetime
            version.sent_at = datetime.utcnow()
            financial_year.tb_status = "shared"
            db.session.commit()
            outcome["sent"] = True
            outcome["at"] = version.sent_at.isoformat()
            flash(f"Review link emailed to {message['to']}."
                  + (f" Access code: {passcode} — send this separately."
                     if passcode else ""), "success")
        else:
            db.session.commit()
            outcome["error"] = result["error"]
            flash(f"Could not send: {result['error']} The link below still "
                  f"works — copy it to the customer yourself.", "error")
    else:
        db.session.commit()
        outcome["error"] = ("Email is not configured on this server "
                            "(set MAIL_USERNAME and MAIL_PASSWORD in .env).")
        flash("Email isn't configured, so nothing was sent. Copy the link "
              "below to the customer."
              + (f" Access code: {passcode}" if passcode else ""), "warning")

    # Shown once, then never again - the raw token is not stored.
    session["review_link_once"] = url
    session["review_send_once"] = outcome
    if passcode:
        session["review_passcode_once"] = passcode

    return redirect(url_for("trial_balance.review", fy_id=fy_id))


@bp.route("/fy/<int:fy_id>/revoke-link", methods=["POST"])
@login_required
def revoke_link(fy_id):
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)
    from ..services import tb_review

    link = financial_year.active_review_link
    if link is None:
        flash("There is no active link to revoke.", "warning")
    else:
        tb_review.revoke(link, user_id=current_user.id)
        flash("Link revoked. It will no longer open for the customer.",
              "success")
    return redirect(url_for("trial_balance.review", fy_id=fy_id))


@bp.route("/api/change/<int:change_id>", methods=["POST"])
@login_required
def decide_change(change_id):
    """Accept or reject one proposed change."""
    from ..services import tb_review

    payload = request.get_json(silent=True) or {}
    result = tb_review.decide(
        change_id,
        payload.get("decision"),
        applied_value=payload.get("applied_value"),
        note=payload.get("note"),
        user_id=current_user.id)

    return jsonify(result), (200 if result.get("ok") else 400)


@bp.route("/version/<int:version_id>/decide-all", methods=["POST"])
@login_required
def decide_all(version_id):
    from ..models import TrialBalanceVersion
    from ..services import tb_review

    version = db.session.get(TrialBalanceVersion, version_id) or abort(404)
    decision = request.form.get("decision")

    if decision not in ("accepted", "rejected"):
        abort(400)

    count = tb_review.decide_all(version, decision, user_id=current_user.id)
    flash(f"{count} change(s) {decision}.", "success")
    return redirect(url_for("trial_balance.review",
                            fy_id=version.financial_year_id))


@bp.route("/version/<int:version_id>/finish", methods=["POST"])
@login_required
def finish_review(version_id):
    from ..models import TrialBalanceVersion
    from ..services import tb_review

    version = db.session.get(TrialBalanceVersion, version_id) or abort(404)
    result = tb_review.finish_review(version, user_id=current_user.id)

    if result.get("ok"):
        flash("Review round closed. The trial balance now reflects your "
              "decisions — approve it to regenerate the statements.", "success")
        return redirect(url_for("trial_balance.index",
                                fy_id=version.financial_year_id))

    flash(result.get("error", "Could not close the review."), "error")
    return redirect(url_for("trial_balance.review",
                            fy_id=version.financial_year_id))
