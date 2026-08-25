"""Encryption for credentials held on behalf of clients.

A Xero refresh token is sixty days of standing read access to a client's
complete accounting records. It does not belong in a database column in
plaintext, where a stray backup, a log dump or a read-only DB user would
expose every client's books at once.

Fernet (AES-128-CBC with an HMAC) is used rather than anything home-made.
The key comes from TOKEN_ENCRYPTION_KEY in .env; if that is not set it is
derived from SECRET_KEY so the app still runs on a development machine - but
`flask check-xero` says so, because a derived key means rotating SECRET_KEY
would lock every stored token out.
"""
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

log = logging.getLogger(__name__)


def _fernet() -> Fernet:
    key = (current_app.config.get("TOKEN_ENCRYPTION_KEY") or "").strip()

    if not key:
        # Derived, not random: the same SECRET_KEY must always produce the
        # same encryption key, or existing tokens become unreadable.
        secret = current_app.config.get("SECRET_KEY", "")
        digest = hashlib.sha256(f"auditmate-token::{secret}".encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()

    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError):
        # A key that was set but is not valid Fernet material. Fall back to a
        # derivation of it rather than refusing to start, and say so.
        log.warning("TOKEN_ENCRYPTION_KEY is not a valid Fernet key; "
                    "deriving one from it instead. Generate a proper key "
                    "with: python -c \"from cryptography.fernet import "
                    "Fernet; print(Fernet.generate_key().decode())\"")
        digest = hashlib.sha256(key.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def using_derived_key() -> bool:
    """True when no TOKEN_ENCRYPTION_KEY is set, so the key follows SECRET_KEY."""
    return not (current_app.config.get("TOKEN_ENCRYPTION_KEY") or "").strip()


def encrypt(value: str) -> str:
    """Encrypt a secret for storage. Empty input stays empty."""
    if not value:
        return ""
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    """Decrypt a stored secret.

    Returns "" when the value cannot be read - which happens if the key
    changed. The caller treats that as "not connected" and asks the auditor
    to reconnect, rather than crashing with a stack trace.
    """
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        log.warning("Stored token could not be decrypted - the encryption "
                    "key has changed. The connection must be re-established.")
        return ""
