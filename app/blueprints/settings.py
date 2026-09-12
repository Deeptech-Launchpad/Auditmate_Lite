"""The firm's standing disclosure wording, and any client that departs from it.

Partner-only, like the users screen: these five answers appear verbatim in
every client's financial statements, so changing one silently rewords notes
across every engagement the firm has open. That is a partner's decision.

See services/disclosure_settings.py for what the five are and why they have
to be answered by a person rather than derived.
"""
from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (DISCLOSURE_SETTING_KEYS, Customer,
                      DisclosureSetting)
from ..services import disclosure_settings
from ..services.audit import record
from ..services.permissions import partner_required

bp = Blueprint("settings", __name__, url_prefix="/settings")


def _posted():
    return {key: request.form.get(key, "") for key in DISCLOSURE_SETTING_KEYS}


@bp.route("/disclosures", methods=["GET", "POST"])
@login_required
@partner_required
def disclosures():
    """The firm-wide answers, used by every client that has not overridden."""
    if request.method == "POST":
        changed = disclosure_settings.save(_posted(), customer_id=None,
                                           user_id=current_user.id)
        record("firm", 0, "disclosure_settings",
               after={"changed": changed}, commit=True)
        flash(f"Firm disclosure wording saved ({changed} change"
              f"{'' if changed == 1 else 's'}). Notes across every open "
              f"engagement will use it from the next report build.",
              "success")
        return redirect(url_for("settings.disclosures"))

    return render_template(
        "settings/disclosures.html",
        rows=disclosure_settings.rows_for_form(),
        customer=None,
        # Which clients have said something different, so a partner can see
        # at a glance that the firm default is not universal.
        overriding=(Customer.query
                    .filter(Customer.id.in_(
                        db.session.query(DisclosureSetting.customer_id)
                        .filter(DisclosureSetting.customer_id.isnot(None))))
                    .order_by(Customer.name).all()),
    )


@bp.route("/disclosures/customer/<int:customer_id>", methods=["GET", "POST"])
@login_required
@partner_required
def customer_disclosures(customer_id):
    """One client departing from the firm's standing wording.

    A blank box here means "use the firm's answer" - it is not the same as
    an empty answer, and `disclosure_settings.save` deletes the row rather
    than storing a blank so the inheritance is real rather than a copy.
    """
    customer = db.session.get(Customer, customer_id) or abort(404)

    if request.method == "POST":
        changed = disclosure_settings.save(_posted(), customer_id=customer.id,
                                           user_id=current_user.id)
        record("customer", customer.id, "disclosure_settings",
               after={"changed": changed}, commit=True)
        flash(f"Disclosure wording saved for {customer.name}.", "success")
        return redirect(url_for("settings.customer_disclosures",
                                customer_id=customer.id))

    return render_template(
        "settings/disclosures.html",
        rows=disclosure_settings.rows_for_form(customer),
        customer=customer,
        overriding=[],
    )
