"""Accounting software connections: connect, choose organisation, pull.

The OAuth handshake lives here. Three things it must get right:

* The auditor's Xero password is never seen by Auditmate - it is typed on
  Xero's own login page and we only ever receive a short-lived code.
* The `state` parameter is minted here, kept in the session, and checked on
  the way back. Without that check, another site could drive an auditor's
  browser through a connect flow and attach an attacker's Xero organisation
  to a customer record.
* A failed connection says what to do next, because "HTTP 401" is not an
  instruction.
"""
import logging

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, session, url_for)
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Customer, FinancialYear
from ..services import xero as xero_service
from ..services.audit import record

log = logging.getLogger(__name__)

bp = Blueprint("integrations", __name__, url_prefix="/integrations")

STATE_KEY = "xero_oauth_state"
CUSTOMER_KEY = "xero_oauth_customer"
RETURN_KEY = "xero_oauth_return"


def _back_to(fallback):
    """Where to return the auditor after a connect round-trip."""
    return session.pop(RETURN_KEY, None) or fallback


# --------------------------------------------------------------------------
# Connect
# --------------------------------------------------------------------------

@bp.route("/xero/connect/<int:customer_id>", methods=["POST"])
@login_required
def connect(customer_id):
    """Start the handshake: send the auditor to Xero to sign in and consent."""
    customer = db.session.get(Customer, customer_id) or abort(404)

    if not xero_service.available():
        flash("Xero is not configured on this server. Add XERO_CLIENT_ID and "
              "XERO_CLIENT_SECRET to .env, or switch on XERO_DEMO_MODE to try "
              "the flow without credentials.", "error")
        return redirect(_back_to(url_for("customers.detail",
                                         customer_id=customer_id)))

    state = xero_service.new_state()
    session[STATE_KEY] = state
    session[CUSTOMER_KEY] = customer.id
    if request.form.get("return_to"):
        session[RETURN_KEY] = request.form["return_to"]

    if xero_service.demo_mode():
        # Demo mode stands in for Xero's login page. It never pretends a real
        # connection exists - the UI labels every demo connection as such.
        return redirect(url_for("integrations.callback",
                                code="demo-code", state=state))

    return redirect(xero_service.authorize_url(state))


@bp.route("/xero/callback")
@login_required
def callback():
    """Xero sends the auditor back here with a one-time code."""
    fallback = url_for("customers.index")

    if request.args.get("error"):
        flash(f"Xero sign-in was cancelled or refused: "
              f"{request.args.get('error_description') or request.args['error']}",
              "error")
        return redirect(_back_to(fallback))

    expected = session.pop(STATE_KEY, None)
    customer_id = session.pop(CUSTOMER_KEY, None)

    # Constant-time comparison is overkill for a value we generated, but the
    # check itself is not optional: it is what ties this callback to a
    # connect the auditor actually started.
    if not expected or request.args.get("state") != expected:
        flash("That Xero sign-in could not be verified, so nothing was "
              "connected. Start again from the customer's page.", "error")
        return redirect(_back_to(fallback))

    customer = db.session.get(Customer, customer_id) if customer_id else None
    if customer is None:
        flash("The customer this sign-in belonged to no longer exists.", "error")
        return redirect(fallback)

    code = request.args.get("code")
    if not code:
        flash("Xero did not return an authorisation code.", "error")
        return redirect(_back_to(fallback))

    try:
        payload = xero_service.exchange_code(code)
    except Exception as exc:                        # noqa: BLE001
        log.exception("Xero code exchange failed")
        flash(f"Could not complete the Xero connection: {exc}", "error")
        return redirect(_back_to(fallback))

    connection = xero_service.save_grant(customer.id, payload,
                                         user_id=current_user.id)
    record("connection", connection.id, "connected",
           after={"customer": customer.id, "provider": "xero"}, commit=True)

    return redirect(url_for("integrations.choose", customer_id=customer.id))


# --------------------------------------------------------------------------
# Choose which organisation
# --------------------------------------------------------------------------

@bp.route("/xero/organisation/<int:customer_id>", methods=["GET", "POST"])
@login_required
def choose(customer_id):
    """Pick which Xero organisation is this customer.

    One Xero login can reach every client an audit firm has been invited to,
    so this step is what says "these books belong to Marina Bay Trading".
    """
    customer = db.session.get(Customer, customer_id) or abort(404)
    connection = xero_service.get_connection(customer_id)

    if connection is None:
        flash("This customer is not connected to Xero yet.", "error")
        return redirect(url_for("customers.detail", customer_id=customer_id))

    if request.method == "POST":
        tenant_id = request.form.get("tenant_id")
        tenant_name = request.form.get("tenant_name")
        if not tenant_id:
            flash("Choose an organisation to continue.", "error")
            return redirect(url_for("integrations.choose",
                                    customer_id=customer_id))

        xero_service.choose_tenant(connection, tenant_id, tenant_name)
        record("connection", connection.id, "tenant_selected",
               after={"tenant": tenant_name}, commit=True)
        flash(f"{customer.name} is now linked to the Xero organisation "
              f"“{tenant_name}”.", "success")
        return redirect(_back_to(url_for("customers.detail",
                                         customer_id=customer_id)))

    try:
        tenants = xero_service.list_tenants(connection)
    except Exception as exc:                        # noqa: BLE001
        log.exception("Could not list Xero tenants")
        flash(f"Could not list your Xero organisations: {exc}", "error")
        return redirect(url_for("customers.detail", customer_id=customer_id))

    # One organisation and nothing chosen yet: no decision to make.
    if len(tenants) == 1 and not connection.tenant_id:
        only = tenants[0]
        xero_service.choose_tenant(connection, only["tenant_id"], only["name"])
        flash(f"{customer.name} is now linked to the Xero organisation "
              f"“{only['name']}”.", "success")
        return redirect(_back_to(url_for("customers.detail",
                                         customer_id=customer_id)))

    return render_template("integrations/choose.html",
                           customer=customer, connection=connection,
                           tenants=tenants,
                           demo=xero_service.demo_mode())


@bp.route("/xero/disconnect/<int:customer_id>", methods=["POST"])
@login_required
def disconnect(customer_id):
    customer = db.session.get(Customer, customer_id) or abort(404)
    connection = xero_service.get_connection(customer_id)

    if connection is not None:
        record("connection", connection.id, "disconnected",
               before={"tenant": connection.tenant_name}, commit=True)
        xero_service.disconnect(connection)

    flash(f"Disconnected {customer.name} from Xero. Auditmate has forgotten "
          f"its access. To withdraw consent completely, remove Auditmate "
          f"from the connected apps inside Xero as well.", "success")
    return redirect(_back_to(url_for("customers.detail",
                                     customer_id=customer_id)))


# --------------------------------------------------------------------------
# Pull
# --------------------------------------------------------------------------

@bp.route("/xero/pull-prior/<int:fy_id>", methods=["POST"])
@login_required
def pull_prior(fy_id):
    """Fetch LAST year's trial balance, a year to the day before this one.

    Last year's closing balances are needed four times over - the comparative
    column, the opening-balance check, the movement review and last year's
    mapping - and two of those are data rather than checks: without them the
    statements cannot be issued at all.

    Xero holds the whole history, so this costs one request and involves no
    reading. It does not replace last year's signed accounts: those say what
    was REPORTED, while this says what the books hold today. Having both is
    what makes the opening-balance check possible.
    """
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)
    back = url_for("documents.index", fy_id=fy_id)

    result = xero_service.pull(financial_year, user_id=current_user.id,
                               prior=True)
    if not result.get("ok"):
        flash(result.get("error", "The prior-year pull failed."), "error")
        return redirect(back)

    flash(f"Pulled {result.get('accounts', 0)} account(s) as at "
          f"{result['as_at']:%d %b %Y}. These are last year's figures - they "
          f"do not change this year's trial balance.", "success")
    return redirect(back)


@bp.route("/xero/pull/<int:fy_id>", methods=["POST"])
@login_required
def pull(fy_id):
    """Fetch the trial balance for this engagement's year end."""
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)
    back = url_for("documents.index", fy_id=fy_id)

    result = xero_service.pull(financial_year, user_id=current_user.id)

    if not result.get("ok"):
        flash(result.get("error", "The Xero pull failed."), "error")
        return redirect(back)

    build = result.get("build") or {}
    detail = ""
    if build.get("ok"):
        detail = (f" The standard trial balance now holds "
                  f"{build.get('accounts', 0)} account(s)"
                  + (f", {build['unmapped']} unmapped."
                     if build.get("unmapped") else ", all mapped."))

    flash(f"Pulled {result['accounts']} account(s) from Xero as at "
          f"{result['as_at']:%d %B %Y}."
          + (" The previous pull was replaced." if result.get("replaced") else "")
          + detail, "success")

    record("financial_year", fy_id, "xero_pull",
           after={"accounts": result["accounts"]}, commit=True)

    return redirect(url_for("trial_balance.index", fy_id=fy_id))
