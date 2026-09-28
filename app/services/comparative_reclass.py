"""Moving a comparative figure from one statement line to another, without
touching last year's trial balance or writing a journal (library feedback
A13). See models.ComparativeReclassification for why this exists and what
"applied, not stored on the figure" means.
"""
import logging
from decimal import Decimal

from ..extensions import db
from ..models import ComparativeReclassification

log = logging.getLogger(__name__)

ZERO = Decimal("0")


def for_statement(financial_year_id, statement_type):
    """Every reclassification recorded against this statement, oldest first."""
    return (ComparativeReclassification.query
            .filter_by(financial_year_id=financial_year_id,
                       statement_type=statement_type)
            .order_by(ComparativeReclassification.id).all())


def apply(prior, financial_year_id, statement_type):
    """`prior` ({line_key: amount}), with every recorded reclassification
    for this statement moved. Mutates and returns the same dict.

    Order matters only if two reclassifications touch the same line, in
    which case they are applied in the order they were recorded - the same
    rule an audit trail of sequential corrections always follows.
    """
    for row in for_statement(financial_year_id, statement_type):
        amount = row.amount or ZERO
        prior[row.from_key] = (prior.get(row.from_key) or ZERO) - amount
        prior[row.to_key] = (prior.get(row.to_key) or ZERO) + amount
    return prior


def add(financial_year_id, statement_type, from_key, to_key, amount, reason,
       user_id=None):
    """Record a reclassification. Returns the row.

    Nothing here checks the two keys are on the same statement, or that
    `amount` is no larger than what `from_key` actually holds - the same
    restraint set_figure()/set_paragraph() in overrides.py takes: the
    engine performs no arithmetic and makes no judgement about a
    preparer's own reclassification, it only keeps the record of it.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Say why this is being reclassified.")
    row = ComparativeReclassification(
        financial_year_id=financial_year_id, statement_type=statement_type,
        from_key=from_key, to_key=to_key, amount=amount, reason=reason,
        created_by=user_id)
    db.session.add(row)
    db.session.commit()
    return row


def remove(reclass_id, financial_year_id):
    """Withdraw a reclassification. Deleted outright, not struck through
    (OV-07's "clearing keeps the record" is for what an engine assembled
    and a person typed over; this is a person's own entry from the start,
    and the note paragraph it triggers already carries its own reason)."""
    row = db.session.get(ComparativeReclassification, reclass_id)
    if row is None or row.financial_year_id != financial_year_id:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def any_for_year(financial_year_id):
    """Whether this engagement has any reclassification at all, for the
    library's own comparative-reclassification paragraph (N36_P1) to
    decide whether it applies."""
    return db.session.query(
        ComparativeReclassification.query
        .filter_by(financial_year_id=financial_year_id).exists()).scalar()
