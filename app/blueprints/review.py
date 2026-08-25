"""Public customer review page - NO LOGIN.

This is the only part of the application reachable without authentication, so
it is deliberately narrow:

  * one route family, all keyed on a random token
  * shows exactly one engagement's trial balance and nothing else
  * no navigation into the rest of the app, no account, no password
  * every access is logged with IP and timestamp

Nothing here calls `login_required`, and nothing here should ever expose an
object that was not reached through the token.
"""
import logging

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, session, url_for)

from ..extensions import db
from ..services import tb_review
from ..services.audit import record

log = logging.getLogger(__name__)

bp = Blueprint("review", __name__, url_prefix="/review")


def _client_ip():
    """Caller's IP, honouring one proxy hop (nginx on the VPS)."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return (request.remote_addr or "")[:45]


def _passcode_ok(link) -> bool:
    """Has this browser already cleared the passcode for this link?"""
    if not link.needs_passcode:
        return True
    return session.get(f"review_pass_{link.id}") is True


def _load(token):
    """Resolve a token to a usable link, or render the reason it isn't."""
    link = tb_review.resolve(token)

    if link is None:
        # Same response whether the token never existed or was mistyped -
        # nothing here confirms whether a given token is real.
        return None, render_template("review/unavailable.html",
                                     reason="not_found"), 404

    if link.is_revoked:
        return None, render_template("review/unavailable.html",
                                     reason="revoked"), 410

    if link.is_expired:
        return None, render_template("review/unavailable.html",
                                     reason="expired",
                                     link=link), 410

    return link, None, None


@bp.route("/<token>", methods=["GET"])
def open_review(token):
    """The customer's trial balance page."""
    link, failure, status = _load(token)
    if link is None:
        return failure, status

    if not _passcode_ok(link):
        return render_template("review/passcode.html", token=token)

    tb_review.note_access(link, ip=_client_ip())
    log.info("Review link opened for FY %s from %s",
             link.financial_year_id, _client_ip())

    financial_year = link.financial_year
    accounts = sorted(financial_year.tb_accounts,
                      key=lambda a: (a.account_code or "", a.account_name))

    return render_template(
        "review/trial_balance.html",
        token=token,
        link=link,
        fy=financial_year,
        customer=financial_year.customer,
        accounts=accounts,
        totals=financial_year.tb_totals,
        already_submitted=link.submitted_at is not None,
    )


@bp.route("/<token>/passcode", methods=["POST"])
def submit_passcode(token):
    link = tb_review.resolve(token)
    if link is None or not link.is_usable:
        return render_template("review/unavailable.html",
                               reason="not_found"), 404

    if tb_review.check_passcode(link, request.form.get("passcode")):
        session[f"review_pass_{link.id}"] = True
        return redirect(url_for("review.open_review", token=token))

    log.warning("Bad passcode for review link %s from %s",
                link.id, _client_ip())
    flash("That code was not recognised. Please check and try again.", "error")
    return render_template("review/passcode.html", token=token), 401


@bp.route("/<token>/submit", methods=["POST"])
def submit(token):
    """Take the customer's edits and record them as a new version."""
    link, failure, status = _load(token)
    if link is None:
        return failure, status

    if not _passcode_ok(link):
        return render_template("review/passcode.html", token=token), 401

    # Collect edits, which arrive as account_<id>_<field>.
    edits = {}
    for name, value in request.form.items():
        if not name.startswith("account_"):
            continue
        parts = name.split("_", 2)
        if len(parts) != 3:
            continue
        _, account_id, field = parts
        if field not in ("debit", "credit", "comment"):
            continue
        edits.setdefault(account_id, {})[field] = value

    result = tb_review.submit(
        link, edits,
        message=request.form.get("message"),
        ip=_client_ip())

    record("review_link", link.id, "customer_submit",
           after={"changes": result.get("changes", 0)}, commit=True)

    return render_template("review/submitted.html",
                           fy=link.financial_year,
                           customer=link.financial_year.customer,
                           changes=result.get("changes", 0))
