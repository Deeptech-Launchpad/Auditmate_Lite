"""Authentication: sign in / sign out."""
from datetime import datetime

from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for)
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..models import User
from ..services.audit import record

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        user = User.query.filter_by(email=email).first()

        # Same message either way — don't reveal which accounts exist.
        if user is None or not user.check_password(password):
            flash("Incorrect email or password.", "error")
            return render_template("auth/login.html", email=email), 401

        if not user.is_active:
            flash("This account has been deactivated.", "error")
            return render_template("auth/login.html", email=email), 403

        login_user(user, remember=bool(request.form.get("remember")))
        user.last_login_at = datetime.utcnow()
        record("user", user.id, "login")
        db.session.commit()

        next_page = request.args.get("next")
        # Only allow relative redirects, so ?next= can't bounce to another site.
        if not next_page or not next_page.startswith("/"):
            next_page = url_for("dashboard.index")
        return redirect(next_page)

    return render_template("auth/login.html", email="")


@bp.route("/logout")
@login_required
def logout():
    record("user", current_user.id, "logout", commit=True)
    logout_user()
    flash("You have been signed out.", "success")
    return redirect(url_for("auth.login"))
