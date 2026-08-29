"""Application factory."""
import logging
import os
from datetime import datetime

from flask import Flask, render_template
from markupsafe import Markup

from .config import Config
from .extensions import csrf, db, login_manager, migrate


def _use_system_certificates():
    """Verify TLS against the operating system's certificate store.

    Python HTTP clients normally verify against the `certifi` bundle, which
    contains only public root CAs. That breaks wherever a TLS-inspecting
    proxy sits in the path - corporate gateways, and consumer antivirus such
    as Norton or Kaspersky - because those re-sign every connection with a
    private CA that is installed in the OS trust store but will never appear
    in certifi.

    The symptom is confusing: the OS trusts the connection, so email works,
    while API calls fail with CERTIFICATE_VERIFY_FAILED. Deferring to the OS
    store fixes it without weakening verification - certificates are still
    fully checked, just against the trust store the machine actually uses.

    Never disable verification instead. This application transmits client
    financial data.
    """
    try:
        import truststore
        truststore.inject_into_ssl()
        return True
    except Exception:                              # noqa: BLE001
        # certifi remains the fallback; only inspected connections suffer.
        return False


def create_app(config_object=Config):
    _use_system_certificates()

    app = Flask(__name__)
    app.config.from_object(config_object)

    if app.config.get("TRUST_PROXY"):
        # Behind nginx. Without this Flask sees every request as coming from
        # 127.0.0.1 over http, so url_for(_external=True) - which builds the
        # review links emailed to customers - would hand them an http:// URL
        # even when the site is served over https.
        # Only enable behind a proxy that sets these headers itself;
        # otherwise a client could forge them.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # --- Extensions ---
    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    login_manager.init_app(app)

    # --- Blueprints ---
    from .blueprints.auth import bp as auth_bp
    from .blueprints.dashboard import bp as dashboard_bp
    from .blueprints.customers import bp as customers_bp
    from .blueprints.documents import bp as documents_bp
    from .blueprints.trial_balance import bp as trial_balance_bp
    from .blueprints.statements import bp as statements_bp
    from .blueprints.reports import bp as reports_bp
    # Public, no-login customer review.
    from .blueprints.review import bp as review_bp
    from .blueprints.integrations import bp as integrations_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(customers_bp)
    app.register_blueprint(documents_bp)
    app.register_blueprint(trial_balance_bp)
    app.register_blueprint(statements_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(review_bp)
    app.register_blueprint(integrations_bp)

    # --- Template helpers ---
    from .models import (DOCUMENT_CATEGORIES, ENTITY_TYPES, FY_STATUSES,
                         STATEMENT_TYPES)

    @app.context_processor
    def inject_globals():
        return {
            "DOCUMENT_CATEGORIES": DOCUMENT_CATEGORIES,
            "ENTITY_TYPES": ENTITY_TYPES,
            "STATEMENT_TYPES": STATEMENT_TYPES,
            "FY_STATUSES": FY_STATUSES,
            "AI_ENABLED": app.config.get("AI_ENABLED", False),
            "AI_PROVIDER_LABEL": ("Gemini"
                if app.config.get("AI_PROVIDER") == "gemini"
                else "Claude"),
            "now": datetime.utcnow(),
        }

    # inject_nav_engagements stood here, feeding the Engagement picker in
    # the top bar. The picker is gone at the firm's request - Customers is
    # the way in - and a context processor that queries every open
    # engagement on every page render is not worth keeping for nobody.

    @app.template_filter("money")
    def money(value, dash_on_zero=True):
        """Format a number the way financial statements do."""
        if value is None:
            return "—"
        try:
            amount = float(value)
        except (TypeError, ValueError):
            return "—"
        if amount == 0 and dash_on_zero:
            return "—"
        if amount < 0:
            return f"({abs(amount):,.2f})"
        return f"{amount:,.2f}"

    @app.template_filter("drcr")
    def drcr(value, dash_on_zero=True):
        """Format the way a trial balance is read: a magnitude and a side.

        Internally every balance is held debit-positive, so a payable is a
        negative number. Printing that raw put "89,220.00" from a supplier
        listing beside "-89,220.00" from the trial balance and labelled the
        pair "agrees", which reads as a contradiction and made a correct
        check look broken.

        An auditor does not read balances as signed numbers. They read them
        as debits and credits, and 89,220.00 Cr against a creditors listing
        of 89,220.00 needs no explaining.
        """
        if value is None:
            return "—"
        try:
            amount = float(value)
        except (TypeError, ValueError):
            return "—"
        if amount == 0:
            return "—" if dash_on_zero else "0.00"
        side = "Cr" if amount < 0 else "Dr"
        return Markup('{:,.2f}<span class="side">{}</span>'.format(
            abs(amount), side))

    @app.template_filter("stmt")
    def stmt(value, blank=""):
        """Format the way the published annual report does.

        Whole dollars, thousands separated, negatives in brackets, and a
        double hyphen for nil - which is the Singapore FRS presentation
        convention and what the client's own template uses.
        """
        if value is None:
            return blank or "--"
        try:
            amount = round(float(value))
        except (TypeError, ValueError):
            return blank or "--"
        if amount == 0:
            return "--"
        if amount < 0:
            return "({:,.0f})".format(abs(amount))
        return "{:,.0f}".format(amount)

    @app.template_filter("is_total_row")
    def is_total_row(label):
        """True for a source document's own total line, not an account."""
        from .services.extraction.base import looks_like_total_label
        return looks_like_total_label(label)

    @app.template_filter("datefmt")
    def datefmt(value, fmt="%d %b %Y"):
        if not value:
            return "—"
        return value.strftime(fmt)

    @app.template_filter("pct")
    def pct(value):
        if value is None:
            return "—"
        return f"{float(value) * 100:.0f}%"

    # --- Cache busting for static files ---
    @app.url_defaults
    def stamp_static(endpoint, values):
        """Append the file's modification time to every static URL.

        nginx serves /static/ with `expires 30d`, which is right - these
        files rarely change and should not be re-fetched on every page. But
        it also means that after a deployment a browser keeps using the old
        CSS and JavaScript for a month, and the new UI silently does not
        work. A hard refresh fixes it for whoever knows to do that; nobody
        else finds out.

        Stamping the URL makes the two behave properly together: an
        unchanged file keeps the same URL and stays cached, a changed one
        gets a new URL and is fetched immediately.
        """
        if endpoint != "static" or "filename" not in values:
            return
        try:
            path = os.path.join(app.static_folder, values["filename"])
            values["v"] = int(os.stat(path).st_mtime)
        except OSError:
            pass          # missing file: let the 404 be the visible problem

    # --- Error pages ---
    @app.errorhandler(404)
    def not_found(error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def too_large(error):
        return render_template("errors/413.html"), 413

    @app.errorhandler(500)
    def server_error(error):
        db.session.rollback()
        return render_template("errors/500.html"), 500

    # --- CLI ---
    from .cli import register_cli
    register_cli(app)

    return app
