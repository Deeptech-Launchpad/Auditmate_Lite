"""Customer review of the trial balance, over a no-login link.

The customer receives an emailed URL, opens their trial balance in the
browser, edits it, and submits. Their submission arrives as a new version in
which every altered figure is a separate proposed change, and the auditor
rules on each one individually.

Security model, stated plainly: the token in the URL **is** the credential.
Anyone holding that URL can see the engagement's trial balance. That is the
price of "no login", and the mitigations are:

  * 256 bits of randomness - not guessable
  * only a SHA-256 hash is stored, so a database leak yields no working links
  * expiry, and instant revocation by the auditor
  * constant-time comparison, so lookups leak nothing by timing
  * every access logged with IP and timestamp
  * an optional passcode, delivered by a different channel

The customer page shows one engagement's trial balance and nothing else. No
account, no password, no navigation into the rest of the application.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from argon2 import PasswordHasher
from argon2.exceptions import (InvalidHashError, VerificationError,
                               VerifyMismatchError)
from flask import current_app, url_for

from ..extensions import db
from ..models import (CustomerReviewLink, FinancialYear, TrialBalanceAccount,
                      TrialBalanceChange, TrialBalanceVersion)
from .audit import record

log = logging.getLogger(__name__)

_hasher = PasswordHasher()
ZERO = Decimal("0.00")

DEFAULT_EXPIRY_DAYS = 30


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------

def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_link(financial_year, expires_days=DEFAULT_EXPIRY_DAYS,
                passcode=None, user_id=None):
    """Mint a review link. Returns (link, raw_token, passcode).

    The raw token is returned once and never stored - it goes into the email
    and nowhere else.
    """
    # Any previous link is retired, so only one URL is live at a time.
    for existing in financial_year.review_links:
        if existing.is_usable:
            existing.revoked_at = datetime.utcnow()

    raw_token = secrets.token_urlsafe(32)

    link = CustomerReviewLink(
        financial_year_id=financial_year.id,
        token_hash=_hash_token(raw_token),
        passcode_hash=_hasher.hash(passcode) if passcode else None,
        expires_at=datetime.utcnow() + timedelta(days=expires_days),
        sent_to=financial_year.customer.email,
        created_by=user_id,
    )
    db.session.add(link)
    record("review_link", None, "create",
           after={"expires_days": expires_days,
                  "passcode": bool(passcode)})
    db.session.commit()

    return link, raw_token, passcode


def resolve(raw_token: str):
    """Find the link for a raw token, or None.

    Compared in constant time against the stored hash so a timing difference
    cannot be used to probe for valid tokens.
    """
    if not raw_token or len(raw_token) < 20:
        return None

    candidate = _hash_token(raw_token)
    for link in CustomerReviewLink.query.filter_by(token_hash=candidate).all():
        if secrets.compare_digest(link.token_hash, candidate):
            return link
    return None


def check_passcode(link, passcode) -> bool:
    if not link.passcode_hash:
        return True
    if not passcode:
        return False
    try:
        return _hasher.verify(link.passcode_hash, str(passcode).strip())
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def note_access(link, ip=None):
    link.access_count = (link.access_count or 0) + 1
    link.last_accessed_at = datetime.utcnow()
    link.last_accessed_ip = ip
    db.session.commit()


def revoke(link, user_id=None):
    link.revoked_at = datetime.utcnow()
    record("review_link", link.id, "revoke", commit=True)


def review_url(raw_token: str) -> str:
    return url_for("review.open_review", token=raw_token, _external=True)


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------

def snapshot(financial_year) -> dict:
    """Capture the trial balance exactly as it stands."""
    accounts = sorted(financial_year.tb_accounts,
                      key=lambda a: (a.account_code or "", a.account_name))
    totals = financial_year.tb_totals
    return {
        "captured_at": datetime.utcnow().isoformat(),
        "customer": financial_year.customer.name,
        "year_label": financial_year.year_label,
        "currency": financial_year.customer.books_currency,
        "totals": {"debit": float(totals["debit"]),
                   "credit": float(totals["credit"]),
                   "balanced": totals["balanced"]},
        "accounts": [{
            "id": a.id,
            "account_code": a.account_code,
            "account_name": a.account_name,
            "debit": float(a.debit or 0),
            "credit": float(a.credit or 0),
            "is_adjustment": bool(a.is_adjustment),
        } for a in accounts],
    }


def create_version(financial_year, source="auditor", status="sent",
                   link=None, user_id=None) -> TrialBalanceVersion:
    last = (TrialBalanceVersion.query
            .filter_by(financial_year_id=financial_year.id)
            .order_by(TrialBalanceVersion.version_no.desc()).first())

    version = TrialBalanceVersion(
        financial_year_id=financial_year.id,
        version_no=(last.version_no + 1) if last else 1,
        source=source,
        status=status,
        snapshot=snapshot(financial_year),
        link_id=link.id if link else None,
        created_by=user_id,
    )
    db.session.add(version)
    db.session.commit()
    return version


# --------------------------------------------------------------------------
# Customer submission
# --------------------------------------------------------------------------

def _to_decimal(value):
    if value is None or str(value).strip() == "":
        return ZERO
    try:
        return Decimal(str(value).replace(",", "").strip()).quantize(
            Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def submit(link, edits, message=None, ip=None) -> dict:
    """Record what the customer submitted as a new version.

    `edits` is {account_id: {"debit": str, "credit": str, "comment": str}}.
    Only figures that actually differ become changes, so an untouched form
    produces no proposed edits.
    """
    financial_year = link.financial_year

    version = create_version(financial_year, source="customer",
                             status="customer_submitted", link=link)
    version.submitted_at = datetime.utcnow()
    version.submitted_from_ip = ip
    version.customer_message = (message or "").strip()[:4000] or None

    accounts = {a.id: a for a in financial_year.tb_accounts}
    created = 0
    comments_only = 0

    for raw_id, payload in (edits or {}).items():
        try:
            account = accounts.get(int(raw_id))
        except (TypeError, ValueError):
            continue
        if account is None:
            continue

        comment = (payload.get("comment") or "").strip() or None
        changed_here = 0

        for field in ("debit", "credit"):
            if field not in payload:
                continue

            proposed = _to_decimal(payload.get(field))
            if proposed is None:            # unparseable - ignore, don't guess
                continue

            current = Decimal(str(getattr(account, field) or 0)).quantize(
                Decimal("0.01"))
            if proposed == current:
                continue

            db.session.add(TrialBalanceChange(
                version_id=version.id,
                tb_account_id=account.id,
                account_code=account.account_code,
                account_name=account.account_name,
                field=field,
                value_before=current,
                value_after=proposed,
                customer_comment=comment,
                status="pending",
            ))
            created += 1
            changed_here += 1

        # A comment with no figure change is still worth keeping - the
        # customer querying a balance they did not alter ("check this count")
        # is exactly the kind of thing the auditor needs to see.
        #
        # This tested `created` before, which counts changes across the whole
        # submission: as soon as any one account had a figure change, every
        # later comment-only note was silently dropped.
        if comment and not changed_here:
            db.session.add(TrialBalanceChange(
                version_id=version.id,
                tb_account_id=account.id,
                account_code=account.account_code,
                account_name=account.account_name,
                field="comment",
                value_before=None, value_after=None,
                customer_comment=comment,
                status="pending",
            ))
            comments_only += 1

    link.submitted_at = datetime.utcnow()
    financial_year.tb_status = "customer_submitted"

    record("trial_balance", financial_year.id, "customer_submission",
           after={"version": version.version_no, "changes": created})
    db.session.commit()

    log.info("Customer submitted v%s for FY %s: %s change(s)",
             version.version_no, financial_year.id, created)

    return {"ok": True, "version": version, "changes": created}


# --------------------------------------------------------------------------
# Auditor decisions - accept or reject each change
# --------------------------------------------------------------------------

def decide(change_id, decision, applied_value=None, note=None, user_id=None):
    """Accept or reject one proposed change.

    Accepting writes the figure onto the trial balance. Rejecting records the
    decision and leaves the trial balance untouched - but the proposal stays
    visible, so the conversation with the client is never lost.
    """
    change = db.session.get(TrialBalanceChange, change_id)
    if change is None:
        return {"ok": False, "error": "change not found"}

    if decision not in ("accepted", "rejected", "pending"):
        return {"ok": False, "error": "decision must be accepted or rejected"}

    change.status = decision
    change.decided_by = user_id
    change.decided_at = datetime.utcnow() if decision != "pending" else None
    change.decision_note = (note or "").strip() or None

    if decision == "accepted" and change.field in ("debit", "credit"):
        value = _to_decimal(applied_value) if applied_value not in (None, "") \
            else change.value_after
        if value is None:
            return {"ok": False, "error": "that is not a valid number"}

        change.applied_value = value if applied_value not in (None, "") else None

        account = change.account
        if account is not None:
            setattr(account, change.field, value)
            # A figure the customer proposed and the auditor accepted is a
            # reviewed figure.
            account.needs_review = False

    elif decision == "rejected" and change.field in ("debit", "credit"):
        # Put the original figure back, in case an earlier accept moved it.
        account = change.account
        if account is not None and change.value_before is not None:
            setattr(account, change.field, change.value_before)

    record("trial_balance_change", change.id, decision,
           before={"value": str(change.value_before)},
           after={"value": str(change.effective_value), "note": note})
    db.session.commit()

    financial_year = change.version.financial_year
    totals = financial_year.tb_totals
    return {
        "ok": True,
        "status": change.status,
        "totals": {"debit": float(totals["debit"]),
                   "credit": float(totals["credit"]),
                   "difference": float(totals["difference"]),
                   "balanced": totals["balanced"]},
        "pending": financial_year.pending_tb_changes,
    }


def decide_all(version, decision, user_id=None) -> int:
    """Accept or reject every still-pending change in one go."""
    count = 0
    for change in version.pending_changes:
        result = decide(change.id, decision, user_id=user_id)
        if result.get("ok"):
            count += 1
    return count


def finish_review(version, user_id=None):
    """Close a review round once every change has been decided."""
    if version.pending_changes:
        return {"ok": False,
                "error": f"{len(version.pending_changes)} change(s) still "
                         f"need a decision."}

    version.status = "applied"

    # Closing a round hands the trial balance back to the auditor to approve
    # - unless it is approved already, in which case sending it back to draft
    # would silently revoke the approval the statements and report stand on.
    if version.financial_year.tb_status != "approved":
        version.financial_year.tb_status = "draft"
    record("trial_balance_version", version.id, "review_complete",
           after=version.change_summary, commit=True)
    return {"ok": True}


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def compose_email(financial_year, url, passcode=None) -> dict:
    """Build the message that carries the review link."""
    customer = financial_year.customer
    year_end = (financial_year.end_date.strftime("%d %B %Y")
                if financial_year.end_date else financial_year.year_label)

    greeting = (f"Dear {customer.contact_person}" if customer.contact_person
                else "Dear Sir/Madam")

    passcode_line = ""
    if passcode:
        passcode_line = ("\nWhen you open the link it will ask for a short "
                         "access code. We will provide that separately.\n")

    body = f"""{greeting},

We have prepared the trial balance for {customer.name} for the financial year
ended {year_end}, and would be grateful if you could review it.

Please open this link:

  {url}

There is nothing to install and no account to create - the link opens the
trial balance directly in your browser.
{passcode_line}
On that page you can:

  - check every account balance
  - correct any figure that is wrong
  - add a comment against any line to explain a change
  - submit it back to us

We will review whatever you send and come back to you on anything we need to
discuss. The link expires in {DEFAULT_EXPIRY_DAYS} days.

Kind regards,
{current_app.config.get('SMTP_FROM_NAME', 'AltiusNXT Audit')}
"""

    return {
        "to": customer.email,
        "subject": (f"{customer.name} - Trial Balance "
                    f"{financial_year.year_label} for your review"),
        "body": body,
        "url": url,
    }
