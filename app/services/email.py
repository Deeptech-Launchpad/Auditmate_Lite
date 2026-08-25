"""Email: sending statements out, and picking customer replies back up.

The whole round-trip hangs on one idea - a **tracking token** in the subject
line, e.g. "[AM-2025-0007]". Mail clients preserve the subject when someone
hits Reply, so that token is what lets an incoming message be matched back to
the engagement it belongs to. No portal, no login, no public upload page.

Sending uses Gmail SMTP; reading replies uses Gmail IMAP. Both need the same
account credentials in .env. With no credentials set, the app still composes
everything and shows it to the auditor - it just does not transmit. That way
the flow is fully demonstrable before any mailbox is wired up.
"""
import email as email_lib
import imaplib
import logging
import re
import smtplib
import ssl
from datetime import datetime
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path

from flask import current_app

log = logging.getLogger(__name__)

ATTACHMENT_TYPES = {
    ".xlsx": ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".xls": ("application", "vnd.ms-excel"),
    ".pdf": ("application", "pdf"),
    ".csv": ("text", "csv"),
}


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------

def build_token(financial_year) -> str:
    """Stable per-engagement reference, e.g. AM-2025-0007."""
    prefix = current_app.config.get("EMAIL_TOKEN_PREFIX", "AM")
    year = (financial_year.end_date.year if financial_year.end_date
            else datetime.utcnow().year)
    return f"{prefix}-{year}-{financial_year.id:04d}"


TOKEN_RE = re.compile(r"\[([A-Z]{1,6}-\d{4}-\d{3,6})\]")


def find_token(text: str):
    """Pull a tracking token out of a subject line, if present."""
    if not text:
        return None
    match = TOKEN_RE.search(text)
    return match.group(1) if match else None


def email_enabled() -> bool:
    return bool(current_app.config.get("SMTP_USER")
                and current_app.config.get("SMTP_PASSWORD"))


# --------------------------------------------------------------------------
# Composing
# --------------------------------------------------------------------------

def compose_statements_email(financial_year, version, attachments=None) -> dict:
    """Build the subject and body for a statements-for-review email.

    Returned as a plain dict so the UI can preview it even when sending is
    not configured.
    """
    customer = financial_year.customer
    token = version.token or build_token(financial_year)
    year_end = (financial_year.end_date.strftime("%d %B %Y")
                if financial_year.end_date else financial_year.year_label)

    subject = (f"[{token}] {customer.name} - Financial Statements "
               f"{financial_year.year_label} for your review")

    greeting = f"Dear {customer.contact_person}" if customer.contact_person \
        else "Dear Sir/Madam"

    body = f"""{greeting},

Please find attached the draft financial statements for {customer.name}
for the financial year ended {year_end} (version {version.version_no}).

We would be grateful if you could review them and confirm the figures.

  - The Excel workbook is editable. If any figure needs correcting, please
    enter the correct number in the "Revised Amount" column and add a note in
    the "Comment" column.
  - The PDF is provided for reading only.

When you are done, simply REPLY to this email with the edited workbook
attached. Please leave the subject line unchanged - it carries the reference
{token}, which files your reply against the correct engagement.

If the statements are correct as they stand, a reply saying so is enough.

Kind regards,
{current_app.config.get('SMTP_FROM_NAME', 'AltiusNXT Audit')}
"""

    return {
        "to": customer.email,
        "subject": subject,
        "body": body,
        "token": token,
        "attachments": attachments or [],
    }


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------

def send_email(to_address: str, subject: str, body: str,
               attachments=None) -> dict:
    """Send one message. Returns {"ok": bool, "error": str|None}."""
    if not email_enabled():
        return {"ok": False, "error": "Email is not configured "
                                      "(SMTP_USER / SMTP_PASSWORD not set in .env)"}
    if not to_address:
        return {"ok": False, "error": "No recipient address for this customer"}

    config = current_app.config
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((config.get("SMTP_FROM_NAME", "AltiusNXT Audit"),
                                  config["SMTP_USER"]))
    message["To"] = to_address
    message.set_content(body)

    for path in (attachments or []):
        path = Path(path)
        if not path.exists():
            log.warning("Attachment missing, skipping: %s", path)
            continue
        maintype, subtype = ATTACHMENT_TYPES.get(
            path.suffix.lower(), ("application", "octet-stream"))
        message.add_attachment(path.read_bytes(), maintype=maintype,
                               subtype=subtype, filename=path.name)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"],
                          timeout=30) as server:
            server.starttls(context=context)
            server.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
            server.send_message(message)
        log.info("Sent email to %s (%s)", to_address, subject)
        return {"ok": True, "error": None}

    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "error":
                "Gmail rejected the login. Use a 16-character App Password "
                "(Google Account > Security > 2-Step Verification > App "
                "passwords), not your normal password."}
    except Exception as exc:                       # noqa: BLE001
        log.exception("Send failed")
        return {"ok": False, "error": f"Could not send: {exc}"}


# --------------------------------------------------------------------------
# Receiving
# --------------------------------------------------------------------------

def _decode(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:                              # noqa: BLE001
        return str(value)


def fetch_replies(known_tokens=None, mark_seen: bool = True) -> list:
    """Look in the mailbox for customer replies carrying a tracking token.

    Returns a list of dicts: token, from_address, subject, body, attachments
    (each {"filename", "content"}). Only unread messages are examined, and
    only those whose subject carries a token we recognise.
    """
    if not email_enabled():
        return []

    config = current_app.config
    found = []

    try:
        with imaplib.IMAP4_SSL(config["IMAP_HOST"], config["IMAP_PORT"]) as imap:
            imap.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
            imap.select(config.get("IMAP_FOLDER", "INBOX"))

            status, data = imap.search(None, "UNSEEN")
            if status != "OK":
                return []

            for num in data[0].split():
                # PEEK so a message we end up ignoring stays unread.
                status, msg_data = imap.fetch(num, "(BODY.PEEK[])")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue

                message = email_lib.message_from_bytes(msg_data[0][1])
                subject = _decode(message.get("Subject"))
                token = find_token(subject)

                if not token:
                    continue
                if known_tokens and token not in known_tokens:
                    continue

                _, from_address = parseaddr(message.get("From", ""))

                body_text, attachments = "", []
                for part in message.walk():
                    if part.get_content_maintype() == "multipart":
                        continue
                    filename = part.get_filename()
                    if filename:
                        attachments.append({
                            "filename": _decode(filename),
                            "content": part.get_payload(decode=True) or b"",
                        })
                    elif part.get_content_type() == "text/plain" and not body_text:
                        payload = part.get_payload(decode=True) or b""
                        body_text = payload.decode(
                            part.get_content_charset() or "utf-8",
                            errors="replace")

                found.append({
                    "token": token,
                    "from_address": from_address,
                    "subject": subject,
                    "body": _strip_quoted(body_text),
                    "attachments": attachments,
                    "received_at": datetime.utcnow(),
                })

                if mark_seen:
                    imap.store(num, "+FLAGS", "\\Seen")

    except imaplib.IMAP4.error as exc:
        log.error("IMAP error: %s", exc)
    except Exception:                              # noqa: BLE001
        log.exception("Could not fetch replies")

    return found


def _strip_quoted(text: str) -> str:
    """Drop the quoted original so we keep only what the customer actually wrote."""
    if not text:
        return ""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if re.match(r"^On .+ wrote:$", stripped):
            break
        if stripped.startswith("-----Original Message-----"):
            break
        out.append(line)
    return "\n".join(out).strip()


def test_connection() -> dict:
    """Check SMTP and IMAP credentials without sending anything."""
    if not email_enabled():
        return {"ok": False, "error": "SMTP_USER / SMTP_PASSWORD not set in .env"}

    config = current_app.config
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"],
                          timeout=20) as server:
            server.starttls(context=context)
            server.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": f"SMTP failed: {exc}"}

    try:
        with imaplib.IMAP4_SSL(config["IMAP_HOST"], config["IMAP_PORT"]) as imap:
            imap.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
            imap.select(config.get("IMAP_FOLDER", "INBOX"))
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": f"SMTP works, but IMAP failed: {exc}. "
                                      f"Enable IMAP in Gmail settings."}

    return {"ok": True, "error": None}
