"""Audit trail helper.

Audit software has to be able to answer "who changed this number, from what,
to what, and when". Every correction and status change routes through here.
"""
from flask import request
from flask_login import current_user

from ..extensions import db
from ..models import AuditLog


def record(entity_type: str, entity_id, action: str,
           before=None, after=None, commit: bool = False) -> None:
    """Write one audit-trail entry.

    Values are JSON-serialised, so pass plain dicts of primitives.
    """
    user_id = None
    try:
        if current_user and current_user.is_authenticated:
            user_id = current_user.id
    except Exception:                              # outside a request context
        pass

    ip = None
    try:
        ip = request.remote_addr
    except Exception:
        pass

    db.session.add(AuditLog(
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        before=before,
        after=after,
        ip=ip,
    ))

    if commit:
        db.session.commit()
