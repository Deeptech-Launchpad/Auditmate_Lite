"""Xero connection and trial balance pull.

The whole integration in one place: the OAuth handshake, keeping the tokens
alive, and turning Xero's Trial Balance report into the same rows the
document extractors produce.

Three things about Xero drive the design:

1. **One login, many organisations.** An audit firm is invited into each
   client's Xero as an adviser, so a single AltiusNXT login sees every client
   it has been invited to. The auditor connects once and then says which
   organisation belongs to which customer. That is why `tenant_id` is on the
   connection and travels with every request.

2. **Refresh tokens rotate.** Every refresh returns a NEW refresh token and
   invalidates the old one. Losing the new one silently kills the connection,
   so it is saved in the same transaction as the call that obtained it.

3. **Read-only.** The scopes cannot write. Auditmate can never alter a
   client's books, whatever a bug in this file does.

Demo mode serves a canned response so the flow can be demonstrated and the
adapter tested before credentials exist. It is disabled automatically the
moment real credentials are configured - it can never quietly stand in for a
live connection.
"""
import base64
import logging
import secrets as pysecrets
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import requests
from flask import current_app

from ..extensions import db
from ..models import Connection
from .secrets import decrypt, encrypt

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
API_BASE = "https://api.xero.com/api.xro/2.0"

TIMEOUT = 30
# Refresh a little early rather than racing the expiry mid-request.
REFRESH_MARGIN = timedelta(minutes=2)


class XeroError(Exception):
    """Anything that stops a Xero call succeeding, with a readable message."""


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------

def enabled() -> bool:
    return bool(current_app.config.get("XERO_ENABLED"))


def demo_mode() -> bool:
    return bool(current_app.config.get("XERO_DEMO_MODE"))


def available() -> bool:
    """Can the auditor connect at all - for real or in demo?"""
    return enabled() or demo_mode()


def _client_auth_header() -> dict:
    pair = (f"{current_app.config['XERO_CLIENT_ID']}:"
            f"{current_app.config['XERO_CLIENT_SECRET']}")
    encoded = base64.b64encode(pair.encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


# --------------------------------------------------------------------------
# Step 1 - send the auditor to Xero
# --------------------------------------------------------------------------

def authorize_url(state: str) -> str:
    """Where to send the browser so Xero can take the login and consent.

    The auditor's Xero password is typed on Xero's own page. Auditmate never
    sees it, and never stores it.
    """
    from urllib.parse import quote, urlencode

    params = {
        "response_type": "code",
        "client_id": current_app.config["XERO_CLIENT_ID"],
        "redirect_uri": current_app.config["XERO_REDIRECT_URI"],
        "scope": current_app.config["XERO_SCOPES"],
        # Echoed back unchanged: proof the callback answers a request we
        # actually made, rather than one forged by another site.
        "state": state,
    }
    # quote_via=quote, so the spaces between scopes become %20 and not the +
    # that urlencode uses by default. A + is only a space by the convention of
    # HTML form posts; Xero does not apply it here, and reads the whole scope
    # list as one unrecognised scope name. The symptom is an invalid_scope
    # error page with every individual scope perfectly valid.
    return f"{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}"


def new_state() -> str:
    return pysecrets.token_urlsafe(32)


# --------------------------------------------------------------------------
# Step 2 - swap the code for tokens
# --------------------------------------------------------------------------

def exchange_code(code: str) -> dict:
    """Trade the one-time code from the callback for access and refresh tokens."""
    if demo_mode():
        return _demo_tokens()

    response = requests.post(
        TOKEN_URL,
        headers=_client_auth_header(),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": current_app.config["XERO_REDIRECT_URI"],
        },
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise XeroError(_explain(response, "Could not complete the Xero sign-in"))
    return response.json()


def refresh_tokens(connection: Connection) -> dict:
    """Renew an expired access token.

    Xero rotates the refresh token on every use, so the response must be
    persisted before the next call - see `_store_tokens`.
    """
    if demo_mode():
        return _demo_tokens()

    refresh_token = decrypt(connection.refresh_token_enc or "")
    if not refresh_token:
        raise XeroError("This connection has no usable refresh token. "
                        "Reconnect to Xero.")

    response = requests.post(
        TOKEN_URL,
        headers=_client_auth_header(),
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        connection.status = "expired"
        connection.last_error = _explain(response, "Xero refused to renew "
                                                   "the connection")
        db.session.commit()
        raise XeroError(connection.last_error)
    return response.json()


def _store_tokens(connection: Connection, payload: dict) -> None:
    """Save a token response, encrypted.

    Called immediately after every exchange and refresh. Xero's rotation
    means the previous refresh token is already dead by this point, so this
    must not be deferred.
    """
    now = datetime.utcnow()
    connection.access_token_enc = encrypt(payload.get("access_token", ""))

    if payload.get("refresh_token"):
        connection.refresh_token_enc = encrypt(payload["refresh_token"])
        # Xero refresh tokens last 60 days from issue.
        connection.refresh_expires_at = now + timedelta(days=60)

    connection.access_expires_at = now + timedelta(
        seconds=int(payload.get("expires_in", 1800)))
    connection.scopes = payload.get("scope") or connection.scopes
    connection.status = "connected"
    connection.last_error = None


def _access_token(connection: Connection) -> str:
    """A valid access token, refreshing first if this one is about to expire."""
    if demo_mode():
        return "demo-access-token"

    expires = connection.access_expires_at
    if (not expires) or (expires - REFRESH_MARGIN) <= datetime.utcnow():
        _store_tokens(connection, refresh_tokens(connection))
        db.session.commit()

    token = decrypt(connection.access_token_enc or "")
    if not token:
        raise XeroError("The stored Xero token could not be read. Reconnect "
                        "to Xero.")
    return token


# --------------------------------------------------------------------------
# Step 3 - which organisations did this grant cover?
# --------------------------------------------------------------------------

def list_tenants(connection: Connection) -> list:
    """The client organisations this login can reach.

    An audit firm invited into ten clients' Xero files sees all ten here.
    """
    if demo_mode():
        return _DEMO_TENANTS

    response = requests.get(
        CONNECTIONS_URL,
        headers={"Authorization": f"Bearer {_access_token(connection)}",
                 "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise XeroError(_explain(response, "Could not list your Xero "
                                           "organisations"))

    return [{"tenant_id": row.get("tenantId"),
             "name": row.get("tenantName") or "(unnamed organisation)",
             "type": row.get("tenantType")}
            for row in (response.json() or [])
            if row.get("tenantType") in (None, "ORGANISATION")]


# --------------------------------------------------------------------------
# Step 4 - the trial balance
# --------------------------------------------------------------------------

def fetch_trial_balance(connection: Connection, as_at) -> list:
    """The client's trial balance as at a date, normalised.

    Returns rows shaped exactly like the document extractors produce, so the
    merge into the standard trial balance does not care where they came from:

        {"account_code", "account_name", "debit", "credit", "external_id"}
    """
    if not connection.tenant_id:
        raise XeroError("No Xero organisation has been chosen for this "
                        "customer yet.")

    return parse_trial_balance(raw_trial_balance(connection, as_at))


def raw_trial_balance(connection: Connection, as_at) -> dict:
    """Xero's trial balance report exactly as Xero returns it.

    Split out from fetch_trial_balance so the xero-report command can show
    the report before anything interprets it. When a pull comes back empty or
    wrong, the useful question is what Xero actually sent, and a parsed
    result cannot answer that.
    """
    if not connection.tenant_id:
        raise XeroError("No Xero organisation has been chosen for this "
                        "customer yet.")

    if demo_mode():
        return _demo_trial_balance()

    response = requests.get(
        f"{API_BASE}/Reports/TrialBalance",
        headers={
            "Authorization": f"Bearer {_access_token(connection)}",
            "Xero-tenant-id": connection.tenant_id,
            "Accept": "application/json",
        },
        params={"date": as_at.strftime("%Y-%m-%d")},
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise XeroError(_explain(response, "Could not read the trial "
                                           "balance from Xero"))
    return response.json()


def choose_columns(report: dict):
    """Which two cells hold the year-end balances, read from the headings.

    Xero's trial balance carries two pairs of figures side by side: the
    movement for the period, and the year-to-date position. Taking the first
    pair by position gives the movement - and a trial balance built from
    movements balances to the penny while being wrong, which is exactly the
    prior-year mistake in another costume. Nothing in the totals shows it.

    So read the headings and take the year-to-date pair when it is there.
    For a balance sheet account that is the closing balance; for an income
    or expense account it is the year's total. Together that is a trial
    balance at the year end, which is what an audit needs.

    Returns (debit_index, credit_index, how) - `how` naming the columns
    chosen, so flask xero-report can show the choice rather than assert it.
    """
    header = next((r for r in (report.get("Rows") or [])
                   if r.get("RowType") == "Header"), None)
    if not header:
        return 1, 2, "no headings in the report - took columns 1 and 2"

    labels = [(c.get("Value") or "").strip() for c in (header.get("Cells") or [])]
    lowered = [l.lower() for l in labels]

    def pick(word):
        found = [i for i, l in enumerate(lowered) if word in l]
        if not found:
            return None
        # Year-to-date wins over the period column wherever both exist.
        ytd = [i for i in found
               if "ytd" in lowered[i] or "year to date" in lowered[i]]
        return ytd[0] if ytd else found[0]

    debit, credit = pick("debit"), pick("credit")
    if debit is None or credit is None:
        return 1, 2, (f"headings {labels} name no debit/credit pair - "
                      f"took columns 1 and 2")

    return debit, credit, f"{labels[debit]!r} and {labels[credit]!r}"


def parse_trial_balance(payload: dict) -> list:
    """Flatten Xero's report structure into account rows.

    Xero returns a report as nested Rows: a header row, then Section rows
    each holding the account rows. The account name is the first cell; the
    two figures are whichever columns choose_columns settled on.

    Written defensively: a report shape that does not match is skipped rather
    than guessed at, because a guessed trial balance is worse than none.
    """
    rows = []
    reports = payload.get("Reports") or []
    if not reports:
        return rows

    debit_at, credit_at, how = choose_columns(reports[0])
    log.info("Xero trial balance: reading %s", how)
    widest = max(debit_at, credit_at)

    for section in (reports[0].get("Rows") or []):
        if section.get("RowType") != "Section":
            continue

        for row in (section.get("Rows") or []):
            cells = row.get("Cells") or []
            if (row.get("RowType") not in ("Row", "SummaryRow")
                    or len(cells) <= widest):
                continue
            # Section totals are not accounts.
            if row.get("RowType") == "SummaryRow":
                continue

            name = (cells[0].get("Value") or "").strip()
            if not name:
                continue

            debit = _to_decimal(cells[debit_at].get("Value"))
            credit = _to_decimal(cells[credit_at].get("Value"))
            if debit == 0 and credit == 0:
                continue          # nil accounts add nothing to a trial balance

            rows.append({
                "account_code": _attribute(cells[0], "accountCode")
                                or _attribute(row, "accountCode"),
                "account_name": name,
                "debit": debit,
                "credit": credit,
                "external_id": _attribute(cells[0], "accountID")
                               or _attribute(row, "accountID"),
            })

    return rows


def _attribute(node: dict, want: str):
    """Read one of Xero's Attributes entries by id."""
    for attribute in (node.get("Attributes") or []):
        if attribute.get("Id") == want:
            return attribute.get("Value")
    return None


def _to_decimal(value) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value).replace(",", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _explain(response, prefix: str) -> str:
    """Turn an HTTP failure into something an auditor can act on."""
    if response.status_code in (401, 403):
        return (f"{prefix}: Xero rejected the credentials. The connection may "
                f"have been revoked in Xero, or the app's scopes changed. "
                f"Reconnect to Xero.")
    if response.status_code == 429:
        return (f"{prefix}: Xero's rate limit was hit. Wait a minute and try "
                f"again.")
    detail = ""
    try:
        body = response.json()
        detail = (body.get("Detail") or body.get("error_description")
                  or body.get("error") or "")
    except ValueError:
        detail = (response.text or "")[:200]
    return f"{prefix} (HTTP {response.status_code}). {detail}".strip()


# --------------------------------------------------------------------------
# Demo mode
# --------------------------------------------------------------------------
# A canned organisation and trial balance, in Xero's own response shape, so
# the connect-and-pull flow can be demonstrated and `parse_trial_balance`
# tested without credentials. Deliberately different figures from the seeded
# demo data, so it is obvious which source a row came from.

_DEMO_TENANTS = [
    {"tenant_id": "demo-tenant-0001",
     "name": "Demo Company (SG) - Xero", "type": "ORGANISATION"},
]


def _demo_tokens() -> dict:
    return {"access_token": "demo-access-token",
            "refresh_token": "demo-refresh-token",
            "expires_in": 1800,
            "scope": current_app.config.get("XERO_SCOPES", "")}


def _demo_cell(value, code=None, account_id=None):
    cell = {"Value": value}
    attributes = []
    if code:
        attributes.append({"Id": "accountCode", "Value": code})
    if account_id:
        attributes.append({"Id": "accountID", "Value": account_id})
    if attributes:
        cell["Attributes"] = attributes
    return cell


def _demo_trial_balance() -> dict:
    accounts = [
        ("200", "Sales",                     "", "742,300.00"),
        ("260", "Other Revenue",             "", "18,450.00"),
        ("310", "Cost of Goods Sold",        "281,900.00", ""),
        ("400", "Advertising",               "12,600.00", ""),
        ("404", "Bank Fees",                 "980.00", ""),
        ("412", "Consulting & Accounting",   "16,400.00", ""),
        ("420", "Entertainment",             "3,750.00", ""),
        ("429", "General Expenses",          "9,120.00", ""),
        ("433", "Insurance",                 "6,240.00", ""),
        ("445", "Light, Power, Heating",     "7,830.00", ""),
        ("469", "Rent",                      "54,000.00", ""),
        ("477", "Salaries",                  "196,400.00", ""),
        ("478", "Superannuation / CPF",      "29,460.00", ""),
        ("485", "Subscriptions",             "4,320.00", ""),
        ("493", "Telephone & Internet",      "3,960.00", ""),
        ("710", "Office Equipment",          "48,700.00", ""),
        ("720", "Computer Equipment",        "22,150.00", ""),
        ("610", "Accounts Receivable",       "134,820.00", ""),
        ("620", "Prepayments",               "8,600.00", ""),
        ("630", "Inventory",                 "61,300.00", ""),
        ("090", "Business Bank Account",     "88,540.00", ""),
        ("091", "Petty Cash",                "1,450.00", ""),
        ("800", "Accounts Payable",          "", "96,310.00"),
        ("801", "Accruals",                  "", "14,200.00"),
        ("820", "GST Payable",               "", "11,780.00"),
        ("830", "Provision for Income Tax",  "", "21,400.00"),
        ("900", "Bank Loan",                 "", "150,000.00"),
        ("970", "Share Capital",             "", "100,000.00"),
        # Accumulated losses brought forward - a debit balance, and what
        # makes this demo trial balance actually foot. A demo that does not
        # balance teaches the wrong thing.
        ("960", "Retained Earnings",         "140,520.00", ""),
        ("505", "Income Tax Expense",        "21,400.00", ""),
    ]

    return {
        "Reports": [{
            "ReportID": "TrialBalance",
            "ReportName": "Trial Balance",
            "Rows": [
                {"RowType": "Header",
                 "Cells": [{"Value": "Account"}, {"Value": "Debit"},
                           {"Value": "Credit"}, {"Value": "YTD Debit"},
                           {"Value": "YTD Credit"}]},
                {"RowType": "Section", "Title": "", "Rows": [
                    {"RowType": "Row", "Cells": [
                        _demo_cell(name, code, f"demo-acct-{code}"),
                        _demo_cell(debit), _demo_cell(credit),
                        _demo_cell(debit), _demo_cell(credit),
                    ]}
                    for code, name, debit, credit in accounts
                ]},
            ],
        }],
    }


# --------------------------------------------------------------------------
# Connection lifecycle
# --------------------------------------------------------------------------

def get_connection(customer_id: int, provider: str = "xero"):
    return Connection.query.filter_by(customer_id=customer_id,
                                      provider=provider).first()


def save_grant(customer_id: int, payload: dict, user_id=None,
               provider: str = "xero") -> Connection:
    """Record a completed OAuth grant, replacing any previous one."""
    connection = get_connection(customer_id, provider)
    if connection is None:
        connection = Connection(customer_id=customer_id, provider=provider)
        db.session.add(connection)

    connection.connected_by = user_id
    connection.connected_at = datetime.utcnow()
    _store_tokens(connection, payload)
    db.session.commit()
    return connection


def choose_tenant(connection: Connection, tenant_id: str,
                  tenant_name: str = None) -> None:
    connection.tenant_id = tenant_id
    connection.tenant_name = tenant_name
    connection.status = "connected"
    db.session.commit()


def disconnect(connection: Connection) -> None:
    """Forget the tokens.

    The grant also has to be removed inside Xero for it to be fully revoked -
    the auditor is told this, because deleting a row here does not withdraw
    consent on Xero's side.
    """
    db.session.delete(connection)
    db.session.commit()


# --------------------------------------------------------------------------
# Pulling into the engagement
# --------------------------------------------------------------------------

def pull(financial_year, user_id=None, prior=False) -> dict:
    """Fetch the trial balance and land it in the engagement.

    A pull becomes a Document, exactly as an upload does. That is deliberate:
    everything downstream - the merge into the standard trial balance, the
    dedupe against uploaded figures, the mapping rules, the rebuild-safety
    for auditor adjustments - already works on documents, and a second,
    parallel path into the trial balance would be a second thing to keep
    correct.

    It also means a Xero pull is visible in the Documents list beside the
    files, which is where an auditor looks to answer "where did this figure
    come from".
    """
    from ..models import Document, ExtractedLineItem
    from . import trial_balance as tb_service

    connection = get_connection(financial_year.customer_id)
    if connection is None:
        return {"ok": False, "error": "This customer is not connected to Xero."}

    if financial_year.tb_is_approved and not prior:
        return {"ok": False,
                "error": "The trial balance is approved and locked. Reopen it "
                         "before pulling new figures."}

    as_at = financial_year.end_date
    if not as_at:
        return {"ok": False,
                "error": "This financial year has no end date, so there is no "
                         "date to pull the trial balance as at."}

    if prior:
        # A year earlier, to the day. Xero holds the whole history, so last
        # year's closing position costs one more request and no reading at
        # all - which is worth far more than parsing it out of a PDF.
        #
        # Held apart from this year's pull in every way that matters: its own
        # file_type, a category outside TB_SOURCE_PRECEDENCE so it can never
        # build these accounts, and outside COMPARABLE_CATEGORIES so it is
        # never held line by line against them. It describes a different year.
        try:
            as_at = as_at.replace(year=as_at.year - 1)
        except ValueError:
            # 29 February. The prior year has no such day; the day before is
            # the year end that was actually reported.
            as_at = as_at.replace(year=as_at.year - 1, day=28)

    try:
        rows = fetch_trial_balance(connection, as_at)
    except XeroError as exc:
        connection.last_error = str(exc)
        db.session.commit()
        return {"ok": False, "error": str(exc)}
    except requests.RequestException as exc:
        log.exception("Xero request failed")
        return {"ok": False,
                "error": f"Could not reach Xero: {exc}. Check the server's "
                         f"internet connection and try again."}

    if not rows:
        return {"ok": False,
                "error": f"Xero returned no accounts for {as_at:%d %B %Y}. "
                         f"Check the organisation and the year end date."}

    # A re-pull replaces the previous pull rather than adding a second copy.
    # Uploaded documents are untouched.
    kind = "xero_prior" if prior else "xero"
    previous = (Document.query
                .filter_by(financial_year_id=financial_year.id,
                           file_type=kind)
                .all())
    replaced = len(previous)

    if previous:
        from ..models import TrialBalanceAccount

        # Trial balance rows point back at the document they came from. Cut
        # that link before deleting, or the delete fails on the foreign key.
        # Set to NULL rather than deleting the rows: an auditor may have
        # edited or re-mapped one, and build() replaces source-derived rows
        # on its own while leaving that work intact.
        (TrialBalanceAccount.query
         .filter(TrialBalanceAccount.financial_year_id == financial_year.id)
         .filter(TrialBalanceAccount.source_document_id.in_(
             [d.id for d in previous]))
         .update({"source_document_id": None}, synchronize_session=False))
        db.session.flush()

        for old in previous:
            db.session.delete(old)
        db.session.flush()

    label = connection.tenant_name or "Xero"
    prefix = "Prior year trial balance" if prior else "Trial Balance"
    document = Document(
        financial_year_id=financial_year.id,
        original_filename=f"{label} - {prefix} {as_at:%d %b %Y}",
        stored_filename=f"{kind}__{connection.tenant_id}__{as_at:%Y%m%d}",
        storage_path="(pulled from Xero - no file on disk)",
        file_type=kind,
        mime_type="application/json",
        size_bytes=0,
        category="prior_trial_balance" if prior else "trial_balance",
        extraction_status="extracted",
        extraction_engine="xero-api",
        extraction_confidence=1.0,
        ai_used=False,
        # Verified on arrival. Review & Correct exists to catch misreadings
        # of a document; there is no reading here - these are the client's
        # own ledger balances, delivered as numbers. Checking them would mean
        # checking Xero against itself.
        review_status="verified",
        uploaded_by=user_id,
        reviewed_by=user_id,
        reviewed_at=datetime.utcnow(),
    )
    db.session.add(document)
    db.session.flush()

    for index, row in enumerate(rows):
        db.session.add(ExtractedLineItem(
            document_id=document.id,
            row_index=index,
            raw_label=row["account_name"],
            label=row["account_name"],
            account_code=row.get("account_code"),
            debit=row["debit"] or None,
            credit=row["credit"] or None,
            confidence=1.0,
            needs_review=False,
            status="auto",
            source_ref={"provider": "xero",
                        "tenant": connection.tenant_id,
                        "account_id": row.get("external_id"),
                        "as_at": as_at.isoformat()},
        ))

    connection.last_synced_at = datetime.utcnow()
    connection.last_sync_accounts = len(rows)
    connection.last_error = None
    db.session.commit()

    # Fold the pull into the standard trial balance alongside any uploads.
    # A prior-year pull is not part of this year's accounts and must not
    # touch them - it exists for the comparative column and the check against
    # what was signed.
    build = (None if prior
             else tb_service.build(financial_year.id, user_id=user_id))

    log.info("Xero pull for FY %s: %s accounts as at %s",
             financial_year.id, len(rows), as_at)

    return {"ok": True, "accounts": len(rows), "as_at": as_at,
            "replaced": replaced, "document_id": document.id, "build": build}
