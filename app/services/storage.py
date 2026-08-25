"""File storage.

Uploaded documents are untrusted input, so this module is deliberately strict:
files are renamed to a UUID on disk, the original name is kept only in the
database, and every resolved path is asserted to live inside STORAGE_ROOT
before anything is read or written.
"""
import re
import uuid
from pathlib import Path

from flask import current_app
from werkzeug.utils import secure_filename

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(filename: str) -> str:
    """Reduce a user-supplied name to something safe to put on disk."""
    name = secure_filename(filename or "")
    name = _SAFE_NAME.sub("_", name).strip("._")
    return name[:120] or "upload"


def storage_root() -> Path:
    root = Path(current_app.config["STORAGE_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    return root


def build_path(customer_id: int, financial_year_id: int, filename: str) -> Path:
    """Work out where a new upload should be written.

    Layout: storage/<customer_id>/<financial_year_id>/<uuid>__<safe_name>
    """
    safe = sanitize_filename(filename)
    unique = uuid.uuid4().hex[:12]
    directory = storage_root() / str(customer_id) / str(financial_year_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{unique}__{safe}"


def assert_within_storage(path) -> Path:
    """Guard against path traversal.

    Any path we're about to open must resolve to somewhere inside the storage
    root — otherwise a crafted filename could reach elsewhere on the disk.
    """
    resolved = Path(path).resolve()
    root = storage_root().resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("Refusing to access a path outside storage root")
    return resolved


def save_upload(file_storage, customer_id: int, financial_year_id: int) -> dict:
    """Write an uploaded file to disk and return its metadata."""
    from .extraction.parsers import detect_file_type
    from .extraction import file_sha256

    original = file_storage.filename or "upload"
    destination = build_path(customer_id, financial_year_id, original)
    file_storage.save(destination)

    size = destination.stat().st_size
    max_size = current_app.config.get("MAX_FILE_SIZE", 25 * 1024 * 1024)
    if size > max_size:
        destination.unlink(missing_ok=True)
        raise ValueError(
            f"File exceeds the {max_size // (1024 * 1024)} MB limit")

    return {
        "original_filename": original,
        "stored_filename": destination.name,
        "storage_path": str(destination),
        "file_type": detect_file_type(original),
        "mime_type": file_storage.mimetype,
        "size_bytes": size,
        "sha256": file_sha256(destination),
    }


def is_allowed(filename: str) -> bool:
    suffix = Path(filename or "").suffix.lower()
    return suffix in current_app.config.get("ALLOWED_EXTENSIONS", set())
