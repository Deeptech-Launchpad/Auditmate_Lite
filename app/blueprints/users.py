"""Managing logins - creating them, changing them, and switching one off.

Partner-only throughout. Giving staff the ability to create a login -
including a partner login - would be a bigger privilege than anything else
staff can do in this app, so this whole screen is gated the same way
permanently deleting a customer is. See services.permissions.
"""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

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


@bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@partner_required
def edit(user_id):
    """Change an existing login's name, email, or password.

    Role has its own control already - the dropdown right on the list -
    and is not duplicated here. Password is optional on this form:
    leaving it blank keeps the one the person already has, so a partner
    fixing a typo in someone's name is not forced to also hand them a
    new password they did not ask for.
    """
    user = db.session.get(User, user_id) or abort(404)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        if not name or not email:
            flash("Name and email are required.", "error")
            return render_template("users/edit.html", user=user,
                                   form=request.form), 400

        clash = User.query.filter(User.email == email,
                                  User.id != user.id).first()
        if clash:
            flash(f"“{email}” already has a login.", "error")
            return render_template("users/edit.html", user=user,
                                   form=request.form), 400

        if password and len(password) < 8:
            flash("The password needs to be at least 8 characters.", "error")
            return render_template("users/edit.html", user=user,
                                   form=request.form), 400

        before = {"name": user.name, "email": user.email}
        user.name = name
        user.email = email
        password_changed = bool(password)
        if password_changed:
            user.set_password(password)

        record("user", user.id, "edit", before=before,
              after={"name": name, "email": email,
                     "password_changed": password_changed})
        db.session.commit()

        flash(f"{name}'s login has been updated."
             + (" Password changed too." if password_changed else ""),
             "success")
        return redirect(url_for("users.index"))

    return render_template("users/edit.html", user=user, form={})


@bp.route("/<int:user_id>/delete", methods=["POST"])
@login_required
@partner_required
def delete(user_id):
    """Permanently remove a login that has never actually done anything.

    Deactivate is the everyday tool for a login that is done, and stays
    one: every action a person took keeps their name on it, which is the
    whole point of an audit trail, and around thirty different tables
    point at a user for exactly that reason - an upload, a mapping
    decision, an override, an approval. Erasing the person would either
    orphan that history or, worse, silently take it with them.

    So this exists only for the login that was never real - created by
    mistake, a typo, a test account nobody used - and the database
    itself decides which one that is, not a guess made here: if anything
    anywhere still points at this row, the delete is refused rather than
    forced through.
    """
    user = db.session.get(User, user_id) or abort(404)

    if user.id == current_user.id:
        flash("You cannot delete your own login while signed in with it.",
              "error")
        return redirect(url_for("users.index"))

    name, email = user.name, user.email
    try:
        db.session.delete(user)
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        flash(f"{name}'s login cannot be deleted - there is work recorded "
              f"against it somewhere in the system, and an audit trail "
              f"cannot lose that. Deactivate it instead.", "error")
        return redirect(url_for("users.index"))

    record("user", user_id, "delete", before={"name": name, "email": email})
    db.session.commit()

    flash(f"{name}'s login has been permanently deleted.", "success")
    return redirect(url_for("users.index"))


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
