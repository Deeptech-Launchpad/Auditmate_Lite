"""Where a partner and staff login stop being identical.

Almost everything in this app has no permission check at all, deliberately
- a partner and staff account do the same audit work, and gating routine
screens behind a role would just get in the way. This module exists for
the small number of places that are the exception: permanently destroying
a customer's records, and managing other logins. See models.User.is_partner
for which side of that line an unrecognised legacy role falls on.
"""
from functools import wraps

from flask import abort
from flask_login import current_user


def partner_required(view):
    """Refuse a staff login, not just an anonymous one - @login_required
    still runs first wherever this is used."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_partner:
            abort(403)
        return view(*args, **kwargs)
    return wrapped
