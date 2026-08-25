"""Application configuration.

Reads from .env (see .env.example). One Config class; environment variables
override the defaults so the same code runs locally and on the VPS.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# override=True is deliberate. python-dotenv otherwise leaves any variable
# that already exists in the environment alone, so on a shared server a
# neighbouring project's exported SMTP_USER or GEMINI_API_KEY would silently
# win over this app's own .env - sending client mail from the wrong account
# and billing the wrong API key. .env is this app's single source of truth.
load_dotenv(BASE_DIR / ".env", override=True)


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    # --- Core ---
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
    BASE_DIR = BASE_DIR
    CONFIG_DIR = BASE_DIR / "config"

    # --- Database ---
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@localhost:5432/auditmate_dev",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # --- File storage ---
    STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", str(BASE_DIR / "storage"))).resolve()

    # Whole-request cap (Flask enforces this).
    MAX_CONTENT_LENGTH = 200 * 1024 * 1024      # 200 MB
    # Per-file cap (enforced by our own upload handler).
    MAX_FILE_SIZE = 25 * 1024 * 1024            # 25 MB

    ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv", ".docx", ".pdf",
                          ".png", ".jpg", ".jpeg"}

    # --- AI extraction ---
    # Which engine reads documents the deterministic parsers can't handle.
    # Everything downstream (confidence scoring, Review & Correct, account
    # mapping) is provider-neutral, so this is a one-line switch.
    AI_PROVIDER = os.getenv("AI_PROVIDER", "anthropic").strip().lower()

    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
    ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5").strip()

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()

    # --- Xero (accounting software connection) ---
    # Registered at developer.xero.com. The secret is a server-side secret:
    # Auditmate is a web app that can keep one, so it uses the standard
    # authorisation-code flow rather than PKCE.
    XERO_CLIENT_ID = os.getenv("XERO_CLIENT_ID", "").strip()
    XERO_CLIENT_SECRET = os.getenv("XERO_CLIENT_SECRET", "").strip()

    # Must match a redirect URI registered on the Xero app, exactly.
    XERO_REDIRECT_URI = os.getenv(
        "XERO_REDIRECT_URI",
        "http://localhost:5000/integrations/xero/callback").strip()

    # Read-only. Auditmate can never write to a client's books.
    #   offline_access            - required to get a refresh token at all
    #   accounting.reports.read   - the Trial Balance report
    #   accounting.settings.read  - the chart of accounts (codes and types)
    XERO_SCOPES = os.getenv(
        "XERO_SCOPES",
        "offline_access accounting.reports.read accounting.settings.read"
    ).strip()

    XERO_ENABLED = bool(XERO_CLIENT_ID and XERO_CLIENT_SECRET)

    # Demo mode serves a canned trial balance instead of calling Xero, so the
    # whole connect-and-pull flow can be shown without credentials. It refuses
    # to run when real credentials are present, so it can never quietly stand
    # in for a live connection.
    XERO_DEMO_MODE = _as_bool(os.getenv("XERO_DEMO_MODE"), False) and not XERO_ENABLED

    # Key for encrypting stored OAuth tokens. A refresh token is 60 days of
    # standing access to a client's books - it does not sit in the database in
    # plaintext. Falls back to SECRET_KEY so the app still runs undeployed.
    TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()

    # AI features light up automatically once the selected provider has a key.
    AI_ENABLED = bool(GEMINI_API_KEY if AI_PROVIDER == "gemini"
                      else ANTHROPIC_API_KEY)

    # Rows below this confidence get flagged for auditor review.
    CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.80"))

    # --- Email (Gmail / Google Workspace) ---
    # Sending statements to customers, and picking their replies back up.
    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER", "").strip()
    # Google shows app passwords grouped as "abcd efgh ijkl mnop". People
    # paste them exactly as displayed, so strip every space rather than only
    # the ends - the real password is the 16 characters.
    SMTP_PASSWORD = "".join(os.getenv("SMTP_PASSWORD", "").split())
    SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "AltiusNXT Audit").strip()

    IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
    IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
    IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX").strip()

    # Email features light up automatically once credentials are present,
    # exactly like the Anthropic key. Until then the app composes the message
    # and shows it, but does not send.
    EMAIL_ENABLED = bool(SMTP_USER and SMTP_PASSWORD)

    # Prefix for the tracking token that lets a customer's Reply be matched
    # back to its engagement, e.g. [AM-2025-0007].
    EMAIL_TOKEN_PREFIX = os.getenv("EMAIL_TOKEN_PREFIX", "AM").strip()

    # --- Background jobs ---
    JOBS_INLINE = _as_bool(os.getenv("JOBS_INLINE"), True)

    # --- Session security ---
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _as_bool(os.getenv("SESSION_COOKIE_SECURE"), False)
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 12   # 12 hours
