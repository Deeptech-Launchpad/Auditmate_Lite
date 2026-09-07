"""Managing logins - creating them, and switching one off.

Partner-only throughout. Giving staff the ability to create a login -
including a partner login - would be a bigger privilege than anything else
staff can do in this app, so this whole screen is gated the same way
permanently deleting a customer is. See services.permissions.
"""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import ROLES, ROLE_PARTNER, User
from ..services.audit import record
from ..services.permissions import partner_required

bp = Blueprint("users", __name__, url_prefix="/users")


@bp.route("/")
@login_required
@partner_required
def index():
    users = User.query.order_by(User.name).all()
    return render_template("users/index.html", users=users)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@partner_required
def create():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        role = request.form.get("role") or ROLE_PARTNER
        valid_roles = {key for key, _label in ROLES}

        if not name or not email:
            flash("Name and email are required.", "error")
            return render_template("users/form.html", form=request.form), 400
        if role not in valid_roles:
            flash("That is not a real role.", "error")
            return render_template("users/form.html", form=request.form), 400
        if len(password) < 8:
            flash("The password needs to be at least 8 characters.", "error")
            return render_template("users/form.html", form=request.form), 400
        if User.query.filter_by(email=email).first():
            flash(f"“{email}” already has a login.", "error")
            return render_template("users/form.html", form=request.form), 400

        user = User(name=name, email=email, role=role)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        record("user", user.id, "create", after={"name": name, "email": email,
                                                 "role": role})
        db.session.commit()

        flash(f"Login created for {name} ({email}).", "success")
        return redirect(url_for("users.index"))

    return render_template("users/form.html", form={})


@bp.route("/<int:user_id>/role", methods=["POST"])
@login_required
@partner_required
def change_role(user_id):
    """Change an existing login's role - the piece missing at first: a
    role could only ever be chosen when a login was created, never after.
    Needed as soon as this app had to migrate its old "admin"/"auditor"
    logins onto partner/staff, not just new ones going forward.
    """
    user = db.session.get(User, user_id) or abort(404)
    role = request.form.get("role") or ""
    valid_roles = {key for key, _label in ROLES}

    if role not in valid_roles:
        flash("That is not a real role.", "error")
        return redirect(url_for("users.index"))

    if user.id == current_user.id:
        flash("You cannot change your own role while signed in with it - "
              "ask another partner to change it for you.", "error")
        return redirect(url_for("users.index"))

    if user.role == role:
        return redirect(url_for("users.index"))

    before = user.role
    user.role = role
    record("user", user.id, "change_role",
           before={"role": before}, after={"role": role})
    db.session.commit()

    flash(f"{user.name} is now {role}.", "success")
    return redirect(url_for("users.index"))


@bp.route("/<int:user_id>/deactivate", methods=["POST"])
@login_required
@partner_required
def deactivate(user_id):
    """Switch a login off. Nothing about the person's past work changes -
    every action they took keeps their name on it in the audit log."""
    user = db.session.get(User, user_id) or abort(404)

    if user.id == current_user.id:
        flash("You cannot deactivate your own login while signed in with "
              "it.", "error")
        return redirect(url_for("users.index"))

    user.is_active_flag = False
    record("user", user.id, "deactivate", before={"name": user.name})
    db.session.commit()

    flash(f"{user.name}'s login has been deactivated.", "success")
    return redirect(url_for("users.index"))


@bp.route("/<int:user_id>/reactivate", methods=["POST"])
@login_required
@partner_required
def reactivate(user_id):
    user = db.session.get(User, user_id) or abort(404)

    user.is_active_flag = True
    record("user", user.id, "reactivate", before={"name": user.name})
    db.session.commit()

    flash(f"{user.name}'s login has been reactivated.", "success")
    return redirect(url_for("users.index"))
