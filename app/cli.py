"""Command-line commands: `flask init-db`, `flask create-admin`, `flask seed-demo`."""
import random
from datetime import date, datetime, timedelta

import click
from flask import current_app
from flask.cli import with_appcontext
from sqlalchemy import text

from .extensions import db


def register_cli(app):
    app.cli.add_command(init_db)
    app.cli.add_command(sync_schema)
    app.cli.add_command(create_admin)
    app.cli.add_command(seed_demo)
    app.cli.add_command(reset_db)
    app.cli.add_command(check_config)
    app.cli.add_command(check_email)
    app.cli.add_command(check_ai)
    app.cli.add_command(check_xero)
    app.cli.add_command(setup_production)


@click.command("init-db")
@with_appcontext
def init_db():
    """Create all database tables."""
    db.create_all()
    click.echo("Database tables created.")


@click.command("sync-schema")
@click.option("--apply", "do_apply", is_flag=True,
              help="Actually run the statements. Without this it only reports.")
@with_appcontext
def sync_schema(do_apply):
    """Add columns the models have and the database does not.

    `init-db` calls create_all(), which creates missing TABLES but will never
    alter an existing one - so a release that adds a column leaves the
    database a version behind, and the app fails with UndefinedColumn on the
    first query that touches it.

    Alembic is the proper answer and is not initialised in this project, so
    this is the narrow substitute: compare the models against the live
    schema and add what is missing. It only ever ADDS, and only columns that
    are nullable or carry a default - never a NOT NULL column with no
    default, which would fail on any table that already has rows, and never
    a drop or a type change, which could lose client data.
    """
    from sqlalchemy import inspect

    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    dialect = db.engine.dialect

    planned, skipped = [], []

    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue                      # create_all() handles whole tables
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in have:
                continue
            unsafe = (not column.nullable
                      and column.default is None
                      and column.server_default is None)
            if unsafe:
                skipped.append((table.name, column.name,
                                "NOT NULL with no default"))
                continue
            type_sql = column.type.compile(dialect)
            planned.append((table.name, column.name, type_sql))

    if not planned and not skipped:
        click.echo("Schema is up to date - nothing to add.")
        return

    for table_name, column_name, type_sql in planned:
        click.echo(f"  + {table_name}.{column_name}  {type_sql}")
    for table_name, column_name, why in skipped:
        click.echo(f"  ! {table_name}.{column_name} skipped - {why}")

    if not do_apply:
        click.echo("")
        click.echo(f"{len(planned)} column(s) to add. "
                   f"Re-run with --apply to make the change.")
        return

    for table_name, column_name, type_sql in planned:
        db.session.execute(text(
            f'ALTER TABLE "{table_name}" '
            f'ADD COLUMN IF NOT EXISTS "{column_name}" {type_sql}'))
    db.session.commit()

    # Whole tables that are new still need creating.
    db.create_all()
    click.echo("")
    click.echo(f"Added {len(planned)} column(s). Existing rows keep NULL "
               f"until something writes to them.")


@click.command("reset-db")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@with_appcontext
def reset_db(yes):
    """Drop and recreate all tables. Destroys all data.

    On PostgreSQL the whole schema is dropped rather than looping over the
    models. `drop_all()` only knows about tables that still have a model, so
    a table left behind by a deleted model survives - and then blocks the
    drop of anything it holds a foreign key to.
    """
    if not yes:
        click.confirm("This deletes ALL data. Continue?", abort=True)

    from sqlalchemy import text

    if db.engine.dialect.name == "postgresql":
        with db.engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        click.echo("Schema dropped (including any orphaned tables).")
    else:
        db.drop_all()

    db.create_all()
    click.echo("Database reset.")


@click.command("create-admin")
@click.option("--name", prompt="Full name")
@click.option("--email", prompt="Email")
@click.option("--password", prompt="Password", hide_input=True,
              confirmation_prompt=True)
@with_appcontext
def create_admin(name, email, password):
    """Create an auditor login."""
    from .models import User

    if User.query.filter_by(email=email.lower()).first():
        click.echo(f"A user with email {email} already exists.")
        return

    user = User(name=name, email=email.lower(), role="admin")
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    click.echo(f"Created admin user {email}")


@click.command("seed-demo")
@with_appcontext
def seed_demo():
    """Load demo data so the app is clickable straight away.

    Creates a login, three Singapore customers, financial years, and one
    fully-worked engagement with documents, extracted line items and
    generated financial statements.
    """
    from .models import (Customer, Document, ExtractedLineItem, FinancialYear,
                         User)
    from .services import trial_balance as tb_service
    from .services.statements import build_all

    # --- Login -------------------------------------------------------------
    user = User.query.filter_by(email="demo@auditmate.sg").first()
    if user is None:
        user = User(name="Demo Auditor", email="demo@auditmate.sg", role="admin")
        user.set_password("demo1234")
        db.session.add(user)
        db.session.commit()
        click.echo("Created login  demo@auditmate.sg / demo1234")

    if Customer.query.count() > 0:
        click.echo("Customers already exist — skipping demo data.")
        return

    # --- Customers ---------------------------------------------------------
    customers_spec = [
        dict(name="Marina Bay Trading Pte Ltd", uen="201812345K",
             entity_type="private_limited", contact_person="Tan Wei Ming",
             email="finance@marinabaytrading.sg", phone="+65 6221 8890",
             address_line1="12 Marina Boulevard", address_line2="#18-03",
             postal_code="018980", incorporation_date=date(2018, 3, 14),
             directors="Mr Tan Wei Ming\nMs Chua Li Fen",
             company_secretary="Mr Alagappan Arun Ganapathy"),
        dict(name="Orchard Digital Solutions Pte Ltd", uen="202033891M",
             entity_type="private_limited", contact_person="Priya Nair",
             email="accounts@orcharddigital.sg", phone="+65 6733 2214",
             address_line1="391 Orchard Road", address_line2="#22-01",
             postal_code="238873", incorporation_date=date(2020, 7, 2),
             directors="Ms Priya Nair",
             company_secretary="Ms Serene Koh"),
        dict(name="Jurong Engineering LLP", uen="T21LL0456B",
             entity_type="llp", contact_person="Lim Kok Wah",
             email="admin@jurongeng.sg", phone="+65 6899 4410",
             address_line1="8 Jurong Town Hall Road", address_line2=None,
             postal_code="609434", incorporation_date=date(2021, 1, 18),
             directors="Mr Lim Kok Wah\nMr Rajesh Kumar",
             company_secretary="Mr Alagappan Arun Ganapathy"),
    ]

    customers = []
    for spec in customers_spec:
        customer = Customer(created_by=user.id, engagement_partner_id=user.id,
                            financial_year_end_month=12, books_currency="SGD",
                            country="Singapore", **spec)
        db.session.add(customer)
        customers.append(customer)
    db.session.commit()

    # --- Financial years ---------------------------------------------------
    primary = customers[0]

    fy2024 = FinancialYear(customer_id=primary.id, year_label="FY2024",
                           start_date=date(2024, 1, 1), end_date=date(2024, 12, 31),
                           status="report_generated")
    db.session.add(fy2024)
    db.session.commit()

    fy2025 = FinancialYear(customer_id=primary.id, year_label="FY2025",
                           start_date=date(2025, 1, 1), end_date=date(2025, 12, 31),
                           status="in_progress", previous_year_id=fy2024.id)
    db.session.add(fy2025)

    for customer in customers[1:]:
        db.session.add(FinancialYear(
            customer_id=customer.id, year_label="FY2025",
            start_date=date(2025, 1, 1), end_date=date(2025, 12, 31),
            status="in_progress"))
    db.session.commit()

    # --- Documents + extracted line items for the primary engagement -------
    # These stand in for real uploads so the whole pipeline is demonstrable
    # without needing sample files on disk.
    trial_balance_rows = [
        ("Sales",                        None,       485_200.00),
        ("Service Income",               None,        96_400.00),
        ("Other Income",                 None,         4_150.00),
        ("Purchases",                 212_800.00,       None),
        ("Salaries and Wages",         98_600.00,       None),
        ("CPF Contributions",          14_790.00,       None),
        ("Directors' Remuneration",    72_000.00,       None),
        ("Rental Expense",             48_000.00,       None),
        ("Depreciation",               18_450.00,       None),
        ("Audit Fee",                   4_500.00,       None),
        ("Professional Fees",           7_200.00,       None),
        ("Utilities",                   6_840.00,       None),
        ("Telephone and Internet",      2_960.00,       None),
        ("Insurance",                   5_120.00,       None),
        ("Bank Charges",                1_340.00,       None),
        ("Travel and Transport",        8_970.00,       None),
        ("Office Equipment",           42_300.00,       None),
        ("Motor Vehicle",              86_000.00,       None),
        ("Renovation",                 34_500.00,       None),
        ("Trade Debtors",             118_650.00,       None),
        ("Prepayments",                 9_400.00,       None),
        ("Cash at Bank",               76_320.00,       None),
        ("Petty Cash",                  1_200.00,       None),
        ("Inventories",                54_800.00,       None),
        ("Trade Creditors",              None,       88_940.00),
        ("Accruals",                     None,       12_600.00),
        ("Provision for Tax",            None,       15_800.00),
        # The charge as well as the liability, so the tax note reconciles.
        # A trial balance carrying only the provision is out of balance in
        # substance even when it foots.
        ("Income Tax Expense",         15_800.00,       None),
        ("Bank Loan",                    None,      120_000.00),
        ("Share Capital",                None,      100_000.00),
        ("Retained Earnings",            None,       17_450.00),
    ]

    doc = Document(
        financial_year_id=fy2025.id,
        original_filename="FY2025_Trial_Balance.xlsx",
        stored_filename="demo__trial_balance.xlsx",
        storage_path="(demo data — no file on disk)",
        file_type="xlsx", mime_type="application/vnd.openxmlformats-"
                                    "officedocument.spreadsheetml.sheet",
        size_bytes=48_320, category="trial_balance",
        extraction_status="extracted", extraction_engine="openpyxl",
        extraction_confidence=0.95, ai_used=False,
        review_status="verified", uploaded_by=user.id,
        reviewed_by=user.id, reviewed_at=datetime.utcnow(),
    )
    db.session.add(doc)
    db.session.flush()

    for index, (label, debit, credit) in enumerate(trial_balance_rows):
        db.session.add(ExtractedLineItem(
            document_id=doc.id, row_index=index,
            raw_label=label, label=label,
            debit=debit, credit=credit,
            confidence=0.95, needs_review=False,
            source_ref={"sheet": "Trial Balance", "row": index + 2},
            status="auto",
        ))

    # A second document that still needs review — shows the amber flagged
    # rows and the Review & Correct workflow in the demo.
    scanned = Document(
        financial_year_id=fy2025.id,
        original_filename="Vendor_Invoices_Q4_scanned.pdf",
        stored_filename="demo__vendor_invoices.pdf",
        storage_path="(demo data — no file on disk)",
        file_type="pdf", mime_type="application/pdf",
        size_bytes=1_284_400, category="vendor_invoice", page_count=6,
        extraction_status="extracted", extraction_engine="claude",
        extraction_confidence=0.78, ai_used=True,
        review_status="in_review", uploaded_by=user.id,
    )
    db.session.add(scanned)
    db.session.flush()

    invoice_rows = [
        ("Sembawang Logistics Pte Ltd — Freight", 4_820.00, 0.94),
        ("Kian Huat Stationery Supplies",           680.50, 0.91),
        ("SP Group — Electricity Nov",            1_240.75, 0.88),
        ("Cleaning Services — Q4",                2_400.00, 0.72),   # flagged
        ("Ntwrk Solutions Pte Ltd",               3_150.00, 0.61),   # flagged
        ("Unreadable vendor name",                  920.00, 0.44),   # flagged
    ]
    for index, (label, amount, confidence) in enumerate(invoice_rows):
        db.session.add(ExtractedLineItem(
            document_id=scanned.id, row_index=index,
            raw_label=label, label=label, amount=amount,
            confidence=confidence, needs_review=confidence < 0.80,
            source_ref={"page": index + 1}, status="auto",
        ))

    # A third document still queued, to show pipeline states.
    db.session.add(Document(
        financial_year_id=fy2025.id,
        original_filename="Bank_Statement_DBS_Dec2025.pdf",
        stored_filename="demo__bank_statement.pdf",
        storage_path="(demo data — no file on disk)",
        file_type="pdf", mime_type="application/pdf",
        size_bytes=642_100, category="bank_statement",
        extraction_status="queued", review_status="pending",
        uploaded_by=user.id,
    ))

    db.session.commit()

    # --- Build the standard trial balance, and stop there -------------------
    # Deliberately NOT approved, and no statements generated. Approving the
    # trial balance is what produces the statements, so seeding them directly
    # would create a state the real flow cannot reach - statements existing
    # without an approved trial balance behind them. Leaving it here also
    # means the demo starts exactly where the work does.
    tb_service.build(fy2025.id, user_id=user.id)

    click.echo("")
    click.echo("Demo data loaded.")
    click.echo("  Login:     demo@auditmate.sg")
    click.echo("  Password:  demo1234")
    click.echo("")
    click.echo(f"  {len(customers)} customers, "
               f"{FinancialYear.query.count()} financial years, "
               f"{Document.query.count()} documents")
    click.echo("  Marina Bay Trading / FY2025 has a full worked example.")


@click.command("check-config")
@with_appcontext
def check_config():
    """Validate the YAML config files against each other.

    Catches the failure mode where a mapping rule points at a statement line
    that no longer exists - which would otherwise make a figure quietly
    disappear from the accounts.
    """
    from .services.mapping import _seed_rules
    from .services.statements import load_templates
    from .services.reports import load_sections

    templates = load_templates()
    problems = 0

    # Every line key that exists anywhere.
    keys_by_statement = {
        name: {line["key"] for line in (spec.get("lines") or [])}
        for name, spec in templates.items()
    }
    all_keys = set().union(*keys_by_statement.values()) if keys_by_statement else set()

    click.echo("Statement templates:")
    for name, keys in sorted(keys_by_statement.items()):
        click.echo(f"  {name:22} {len(keys)} lines")

    click.echo("")
    click.echo("Mapping rules -> statement lines:")
    for rule in _seed_rules():
        stype, key = rule["statement_type"], rule["line_key"]
        if stype not in templates:
            click.echo(f"  BROKEN  {rule['pattern']!r} -> unknown statement "
                       f"{stype!r}")
            problems += 1
        elif key not in keys_by_statement.get(stype, set()):
            where = " (exists on another statement)" if key in all_keys else ""
            click.echo(f"  BROKEN  {rule['pattern']!r} -> {stype}.{key} "
                       f"does not exist{where}")
            problems += 1

    # Formulas referenced by templates must exist in the registry.
    from .services.compute import FORMULAS
    click.echo("")
    click.echo("Formulas referenced by templates:")
    for name, spec in templates.items():
        for line in (spec.get("lines") or []):
            formula = line.get("formula")
            if formula and formula not in FORMULAS:
                click.echo(f"  MISSING {name}.{line['key']} -> {formula}()")
                problems += 1

    # Report sections that render a statement must name a real one.
    click.echo("")
    click.echo("Report sections:")
    sections = load_sections()
    click.echo(f"  {len(sections)} sections defined")
    for section in sections:
        stype = section.get("statement_type")
        if section.get("type") == "statement" and stype not in templates:
            click.echo(f"  BROKEN  {section['key']} -> unknown statement {stype!r}")
            problems += 1

    click.echo("")
    if problems:
        click.echo(f"{problems} problem(s) found.")
        raise SystemExit(1)
    click.echo("All config cross-references are valid.")


@click.command("check-email")
@click.option("--send-to", default=None,
              help="Also send a real test message to this address.")
@with_appcontext
def check_email(send_to):
    """Verify the Gmail credentials in .env. Sends nothing unless --send-to."""
    from flask import current_app
    from .services.email import test_connection, send_email, email_enabled

    config = current_app.config
    click.echo(f"SMTP  {config.get('SMTP_HOST')}:{config.get('SMTP_PORT')}  "
               f"user={config.get('SMTP_USER') or '(not set)'}")
    click.echo(f"IMAP  {config.get('IMAP_HOST')}:{config.get('IMAP_PORT')}")

    if not email_enabled():
        click.echo("Email is OFF - set SMTP_USER and SMTP_PASSWORD in .env")
        return

    click.echo("")
    click.echo("Checking login (nothing is sent)...")
    result = test_connection()
    if not result["ok"]:
        click.echo(f"  FAILED: {result['error']}")
        raise SystemExit(1)
    click.echo("  OK - SMTP and IMAP both authenticated.")

    if send_to:
        click.echo("")
        click.echo(f"Sending a test message to {send_to} ...")
        sent = send_email(
            send_to,
            "[TEST] Auditmate Lite email check",
            "This is a test message from Auditmate Lite.\n\n"
            "If you can read this, sending works. Reply to this message to "
            "check that reply pick-up works too.\n")
        click.echo("  Sent." if sent["ok"] else f"  FAILED: {sent['error']}")


@click.command("check-ai")
@with_appcontext
def check_ai():
    """Verify the AI provider configured in .env with one small live call."""
    from flask import current_app
    from .services.extraction.ai import test_connection

    config = current_app.config
    provider = config.get("AI_PROVIDER", "anthropic")
    click.echo(f"AI_PROVIDER   {provider}")
    if provider == "gemini":
        click.echo(f"model         {config.get('GEMINI_MODEL')}")
        click.echo(f"key set       {bool(config.get('GEMINI_API_KEY'))}")
    else:
        click.echo(f"model         {config.get('ANTHROPIC_MODEL')}")
        click.echo(f"key set       {bool(config.get('ANTHROPIC_API_KEY'))}")

    click.echo("")
    click.echo("Making one small test request...")
    result = test_connection()
    if result["ok"]:
        click.echo(f"  OK - {result.get('provider')} responded "
                   f"({result.get('model')}).")
    else:
        click.echo(f"  FAILED: {result['error']}")
        raise SystemExit(1)


@click.command("check-xero")
@with_appcontext
def check_xero():
    """Report whether Xero is configured, without calling Xero."""
    from .services import xero as xero_service
    from .services.secrets import using_derived_key
    from .models import Connection

    if xero_service.enabled():
        click.echo("Xero: configured")
        click.echo(f"  client id    {current_app.config['XERO_CLIENT_ID'][:8]}..."
                   f" ({len(current_app.config['XERO_CLIENT_ID'])} chars)")
        click.echo(f"  redirect uri {current_app.config['XERO_REDIRECT_URI']}")
        click.echo(f"  scopes       {current_app.config['XERO_SCOPES']}")
        click.echo("")
        click.echo("  The redirect URI above must be registered on the app at")
        click.echo("  developer.xero.com, character for character.")
    elif xero_service.demo_mode():
        click.echo("Xero: DEMO MODE - no credentials, canned data.")
        click.echo("  The connect-and-pull flow works end to end, but nothing")
        click.echo("  reaches Xero. Set XERO_CLIENT_ID and XERO_CLIENT_SECRET")
        click.echo("  in .env to connect for real.")
    else:
        click.echo("Xero: not configured.")
        click.echo("  Register an app at developer.xero.com, then set")
        click.echo("  XERO_CLIENT_ID and XERO_CLIENT_SECRET in .env.")
        click.echo("  Or set XERO_DEMO_MODE=true to try the flow first.")

    click.echo("")
    if using_derived_key():
        click.echo("Token encryption: derived from SECRET_KEY.")
        click.echo("  Works, but changing SECRET_KEY would lock every stored")
        click.echo("  token out and every customer would have to reconnect.")
        click.echo("  Set TOKEN_ENCRYPTION_KEY in .env. Generate one with:")
        click.echo("    python -c \"from cryptography.fernet import Fernet; "
                   "print(Fernet.generate_key().decode())\"")
    else:
        click.echo("Token encryption: using TOKEN_ENCRYPTION_KEY.")

    connections = Connection.query.all()
    click.echo("")
    click.echo(f"Connections stored: {len(connections)}")
    for connection in connections:
        state = "live" if connection.is_live else connection.status
        click.echo(f"  {connection.customer.name} -> "
                   f"{connection.tenant_name or '(no organisation chosen)'} "
                   f"[{state}]")


@click.command("setup-production")
@click.option("--email", default="jey@deeptechskills.com", show_default=True)
@click.option("--password", default=None,
              help="Omit to be prompted, so it never lands in shell history.")
@click.option("--name", default="Jey", show_default=True)
@click.option("--force", is_flag=True,
              help="Wipe an existing database first. Destroys all data.")
@with_appcontext
def setup_production(email, password, name, force):
    """Prepare a clean install: schema, one admin account, no demo data.

    Separate from seed-demo on purpose. A client install must not carry
    Marina Bay Trading and a demo@auditmate.sg login into production, and
    nothing in this command creates either.
    """
    from .models import User, Customer

    if password is None:
        password = click.prompt("Password", hide_input=True,
                                confirmation_prompt=True)

    if len(password) < 8:
        raise click.ClickException("Use a password of at least 8 characters.")

    if force:
        click.confirm("This deletes ALL existing data. Continue?", abort=True)
        db.session.execute(text("DROP SCHEMA public CASCADE; "
                                "CREATE SCHEMA public;"))
        db.session.commit()

    db.create_all()

    existing = User.query.filter_by(email=email).first()
    if existing:
        existing.name = name
        existing.set_password(password)
        existing.is_active_flag = True
        db.session.commit()
        click.echo(f"Updated the password for {email}.")
    else:
        user = User(name=name, email=email, role="admin",
                    is_active_flag=True)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Created admin {email}.")

    demo = Customer.query.filter(Customer.name.in_([
        "Marina Bay Trading Pte Ltd", "Orchard Digital Solutions Pte Ltd",
        "Jurong Engineering LLP"])).count()
    if demo:
        click.echo("")
        click.echo(f"WARNING: {demo} demo customer(s) are still in this "
                   f"database.")
        click.echo("         Run with --force for a genuinely clean install.")

    click.echo("")
    click.echo(f"Users in this database: {User.query.count()}")
    click.echo(f"Customers:              {Customer.query.count()}")
