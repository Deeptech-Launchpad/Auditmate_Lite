"""The firm's standing answers to questions the notes ask and no figure can.

Five phrases in the FRS library are left blank on purpose:

    "Trade receivables are generally granted credit terms of
     {credit_terms_receivable}."

That is not a figure. No trial balance holds it, no document supplies it,
and nothing can be derived from the numbers - it is the firm stating a
policy. Until it is answered the note prints the placeholder itself into a
client's financial statements, which is the one outcome worth engineering
against.

Two levels, resolved in this order:

    this client's own answer  ->  the firm's default  ->  not set

"Not set" is a real outcome and is reported as one. It is never guessed at
and never silently blanked: `render_bindings` prints "[not set]" in the
preview, the same treatment an unknown binding already gets, so a missing
policy is visible before signing rather than after.
"""
import logging

from ..extensions import db
from ..models import (DISCLOSURE_SETTINGS, DISCLOSURE_SETTING_KEYS,
                      DisclosureSetting)

log = logging.getLogger(__name__)


def firm_defaults():
    """The firm-wide answers, keyed by setting. Missing keys are absent."""
    return {row.key: row.value
            for row in DisclosureSetting.query.filter(
                DisclosureSetting.customer_id.is_(None)).all()
            if (row.value or "").strip()}


def customer_overrides(customer_id):
    """One client's own answers, where it departs from the firm's."""
    if not customer_id:
        return {}
    return {row.key: row.value
            for row in DisclosureSetting.query.filter_by(
                customer_id=customer_id).all()
            if (row.value or "").strip()}


def resolved(customer=None):
    """Every setting's effective value for this client.

    A key absent from the result has no answer at either level - the caller
    decides how to say so, rather than this inventing a plausible one.
    """
    values = firm_defaults()
    values.update(customer_overrides(getattr(customer, "id", None)))
    return values


def save(values, customer_id=None, user_id=None, commit=True):
    """Write answers for one scope: the firm, or one client.

    An empty value DELETES the row rather than storing a blank. For a client
    that means "go back to whatever the firm says", which is the only
    sensible reading of clearing the box - storing an empty string would
    instead override the firm default with nothing at all.
    """
    written = 0
    for key, value in (values or {}).items():
        if key not in DISCLOSURE_SETTING_KEYS:
            continue
        value = (value or "").strip()
        row = DisclosureSetting.query.filter_by(
            customer_id=customer_id, key=key).first()

        if not value:
            if row is not None:
                db.session.delete(row)
                written += 1
            continue

        if row is None:
            row = DisclosureSetting(customer_id=customer_id, key=key)
            db.session.add(row)
        if row.value != value:
            written += 1
        row.value = value
        row.updated_by = user_id

    if commit:
        db.session.commit()
    return written


def rows_for_form(customer=None):
    """What the settings screen renders: one row per setting, with the value
    in force and where it came from.

    The screen has to show more than the value. A blank box on a client's
    page means "uses the firm's answer", and a blank box on the firm's page
    means "nobody has answered this at all" - the same empty box, two
    different meanings, so each row carries which one it is.
    """
    defaults = firm_defaults()
    overrides = customer_overrides(getattr(customer, "id", None))

    rows = []
    for key, label, placeholder, help_text in DISCLOSURE_SETTINGS:
        own = overrides.get(key, "")
        firm = defaults.get(key, "")
        if customer is None:
            source = "firm" if firm else "unset"
            effective = firm
        elif own:
            source, effective = "customer", own
        elif firm:
            source, effective = "firm", firm
        else:
            source, effective = "unset", ""

        rows.append({
            "key": key,
            "label": label,
            "placeholder": placeholder,
            "help": help_text,
            # What is in this scope's own box - blank on a client page means
            # "inherit", which is why it is not the same as `effective`.
            "value": own if customer is not None else firm,
            "firm_value": firm,
            "effective": effective,
            "source": source,
        })
    return rows


def unset_keys(customer=None):
    """Settings with no answer at either level, for this client.

    Reported on the report builder alongside the other content gaps - a note
    carrying an unanswered placeholder is exactly as incomplete as one that
    has never been written.
    """
    values = resolved(customer)
    return [(key, label) for key, label, _p, _h in DISCLOSURE_SETTINGS
            if not (values.get(key) or "").strip()]
