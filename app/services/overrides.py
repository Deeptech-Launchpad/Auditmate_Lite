"""Typing over what the engine assembled, with the reason recorded.

Library 3.5 settled two things that meet here. The engine performs no
arithmetic, so every printed figure is whatever its source said - and a
source can be wrong, late, or superseded by something the preparer knows
and the document does not. So manual entry is available on every figure and
every paragraph (Overrides sheet, OV-01 and OV-02); there is no category of
figure a preparer cannot change.

The price of that is the record. OV-03 says what has to be kept - the
figure as the source gave it, the figure as printed, who, when, and why -
and OV-04 makes the reason compulsory, because the reason is what makes the
override reviewable a year later when the person who made it has forgotten.
This module is the only place that writes one, so there is no path into the
report that skips it.

Three rules that are easy to get wrong and are kept here:

  CLEARING IS NOT DELETING (OV-07). Clearing restores the source figure and
  keeps the record. A withdrawn override is still something a reviewer may
  want to see - very often it is the most interesting thing on the page.

  AN OVERRIDE CHANGES THE ACCOUNTS AND NOTHING ELSE (OV-08). Nothing here
  writes to a trial balance account, a register or a listing. The source
  and the accounts are allowed to differ; what is not allowed is for the
  difference to be invisible.

  IT SURVIVES THE ROLL-FORWARD (OV-06), because this year's figure is next
  year's comparative. Without that, the year-two preparer inherits a number
  with no history and no way to know it was ever changed.

Nothing here calls the AI.
"""
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask_login import current_user

from ..extensions import db
from ..models import (PARAGRAPH_TABLE_INDEX, ReportFigureOverride,
                      ReportOverrideEvent)

log = logging.getLogger(__name__)

class ReasonRequired(ValueError):
    """OV-04: free text, entered at the time. Short is fine; blank is not."""


def _user_id():
    try:
        if current_user and current_user.is_authenticated:
            return current_user.id
    except Exception:                                  # outside a request
        pass
    return None


def _clean(reason):
    text = " ".join(str(reason or "").split())
    if not text:
        raise ReasonRequired(
            "Say why this figure is being changed. The reason is kept with "
            "the override and shown to whoever reviews the draft.")
    return text


def parse_amount(raw):
    """Parse a figure typed into the report. Returns (value, error).

    Cleared means cleared, not nil: an empty box puts the source figure
    back rather than printing a zero.
    """
    if raw is None or str(raw).strip() == "":
        return None, None
    cleaned = str(raw).replace(",", "").replace("−", "-").strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):        # (1,234)
        cleaned = "-" + cleaned[1:-1].strip()
    try:
        return Decimal(cleaned), None
    except InvalidOperation:
        return None, f"{raw!r} is not a number."


def _event(override, action, field, before, after, reason):
    db.session.add(ReportOverrideEvent(
        override=override, action=action, field=field,
        from_value=None if before is None else str(before),
        to_value=None if after is None else str(after),
        reason=reason, user_id=_user_id()))


# --------------------------------------------------------------------------
# Figures
# --------------------------------------------------------------------------

def for_row(section, table_index, row_index):
    return ReportFigureOverride.query.filter_by(
        report_id=section.report_id, section_key=section.section_key,
        table_index=table_index, row_index=row_index).first()


def set_figure(section, table_index, row_index, *, reason,
               label=..., amount=..., anchor_label=None,
               source_amount=None, source_label=None, source_name=None):
    """Type over one row of a note table. Returns the override.

    `label` and `amount` are given only when that field is being changed -
    passing neither is how a caller says "leave it as it is". Passing None
    for one of them clears that field, which restores the source figure.

    `source_amount` is what the row showed before anybody touched it, read
    from the rendered table rather than taken from the browser, so the
    record of the original cannot be edited by the person overriding it.
    """
    reason = _clean(reason)
    override = for_row(section, table_index, row_index)
    fresh = override is None

    if fresh:
        override = ReportFigureOverride(
            report_id=section.report_id, section_key=section.section_key,
            table_index=table_index, row_index=row_index,
            created_by=_user_id())
        db.session.add(override)

    override.anchor_label = anchor_label or override.anchor_label
    # Recorded once, on the way in. A second override of the same row must
    # not overwrite the original with the figure the first one printed.
    if override.source_amount is None and source_amount is not None:
        override.source_amount = source_amount
    if override.source_label is None and source_label is not None:
        override.source_label = source_label
    if source_name and not override.source_name:
        override.source_name = source_name

    touched = False
    if label is not ...:
        text = (label or "").strip() or None
        if text != override.label_override:
            _event(override, "set" if fresh else "changed", "label",
                   override.label_override or override.source_label, text,
                   reason)
            override.label_override = text
            touched = True
    if amount is not ...:
        if amount != override.amount_override:
            _event(override, "set" if fresh else "changed", "amount",
                   override.amount_override if override.amount_override
                   is not None else override.source_amount, amount, reason)
            override.amount_override = amount
            touched = True

    if touched:
        override.reason = reason
        override.updated_by = _user_id()
        override.cleared_at = None

    # An override that never held anything is not history, it is a stray
    # row from a cancelled edit.
    if override.is_empty and not override.events:
        db.session.delete(override)
        db.session.commit()
        return None
    if override.is_empty:
        override.cleared_at = override.cleared_at or datetime.utcnow()

    db.session.commit()
    return override


def clear(override, reason=None):
    """OV-07. Put the source figure back and keep the record of the change."""
    if override is None or not override.is_live:
        return override
    reason = _clean(reason or override.reason)
    for field, value, source in (
            ("amount", override.amount_override, override.source_amount),
            ("label", override.label_override, override.source_label),
            ("wording", override.text_override, override.source_text)):
        if value is not None:
            _event(override, "cleared", field, value, source, reason)
    override.amount_override = None
    override.label_override = None
    override.text_override = None
    override.cleared_at = datetime.utcnow()
    override.updated_by = _user_id()
    db.session.commit()
    return override


# --------------------------------------------------------------------------
# Paragraphs
# --------------------------------------------------------------------------

def _next_paragraph_index(section):
    """A per-section sequence that is never reused.

    Paragraph overrides cannot be addressed by position: paragraphs are
    added, dropped and reordered, and a position that means one sentence
    today would mean another next week. The number here is only a key; what
    ties the override to the sentence is the id written into the wording.
    """
    taken = [row.row_index for row in ReportFigureOverride.query.filter_by(
        report_id=section.report_id, section_key=section.section_key,
        table_index=PARAGRAPH_TABLE_INDEX).all()]
    return (max(taken) + 1) if taken else 0


def set_paragraph(section, *, wording, source_text, reason, para_id=None,
                  override_id=None):
    """Type over one paragraph of a note (OV-02). Returns the override.

    The new wording goes into the section's stored text, the way every
    other edit to a note does - so the export, the carry-forward and the
    search all keep working without knowing overrides exist. What this adds
    is the record beside it: the sentence the library wrote, the sentence
    that prints, who, when and why.
    """
    reason = _clean(reason)
    override = None
    if override_id:
        override = db.session.get(ReportFigureOverride, int(override_id))
        if override is not None and override.report_id != section.report_id:
            override = None
    if override is None and para_id:
        override = ReportFigureOverride.query.filter_by(
            report_id=section.report_id, section_key=section.section_key,
            table_index=PARAGRAPH_TABLE_INDEX, para_id=para_id).first()

    fresh = override is None
    before = source_text if fresh else (override.text_override
                                        or override.source_text)
    if (wording or "").strip() == (before or "").strip():
        return override                        # nothing changed; no record

    if fresh:
        override = ReportFigureOverride(
            report_id=section.report_id, section_key=section.section_key,
            table_index=PARAGRAPH_TABLE_INDEX,
            row_index=_next_paragraph_index(section),
            para_id=para_id, source_name="the notes library",
            source_text=source_text, created_by=_user_id())
        db.session.add(override)
    elif override.source_text is None:
        override.source_text = source_text

    _event(override, "set" if fresh else "changed", "wording",
           before, wording, reason)
    override.text_override = wording
    override.reason = reason
    override.updated_by = _user_id()
    override.cleared_at = None
    db.session.commit()
    return override


def restore_paragraph(override):
    """OV-07 for wording: the library's sentence comes back, record kept."""
    if override is None:
        return None
    source = override.source_text
    clear(override)
    return source


# --------------------------------------------------------------------------
# What the reviewer sees
# --------------------------------------------------------------------------

def for_report(report, include_cleared=True):
    """Every override on a report, newest first, for the reviewer's list.

    OV-05: every override is listed with the original beside it. This is
    what the Preparer checks page prints (PC-10) and what the builder shows
    in its Overrides panel.
    """
    query = ReportFigureOverride.query.filter_by(report_id=report.id)
    rows = query.all()
    if not include_cleared:
        rows = [row for row in rows if row.is_live]

    titles = {s.section_key: s.title for s in report.sections}
    listed = []
    for row in rows:
        listed.append({
            "id": row.id,
            "where": titles.get(row.section_key, row.section_key),
            "what": (row.anchor_label or row.source_label or row.para_id
                     or ("a paragraph" if row.is_paragraph else "a figure")),
            "kind": "wording" if row.is_paragraph else "figure",
            "source_said": (row.source_text if row.is_paragraph
                            else row.source_amount),
            "source_name": row.source_name,
            "now_prints": (row.text_override if row.is_paragraph
                           else row.amount_override),
            "label_now": row.label_override,
            "reason": row.reason,
            "who": row.who,
            "when": row.updated_at or row.created_at,
            "cleared": row.cleared_at is not None,
            "carried": row.carried_from_id is not None,
            "history": [{"action": e.action, "field": e.field,
                         "from": e.from_value, "to": e.to_value,
                         "reason": e.reason, "who": e.who, "at": e.at}
                        for e in row.events],
        })
    listed.extend(_statement_line_overrides(report))
    listed.sort(key=lambda item: (item["cleared"],
                                  -(item["when"].timestamp()
                                    if item["when"] else 0)))
    return listed


def _statement_line_overrides(report):
    """Figures typed over on the face of the statements.

    These have their own home - the line is a stored row, so the override
    lives on it - but a reviewer asking "what did somebody change?" does
    not care which of the two places a figure came from. So they are read
    back into the same list, and the balance sheet and the notes are
    reviewed together.
    """
    from ..models import User

    listed = []
    for statement in getattr(report.financial_year, "statements", []) or []:
        for line in statement.lines:
            if line.manual_override_amount is None and not line.override_reason:
                continue
            person = db.session.get(User, line.override_by) \
                if line.override_by else None
            listed.append({
                "id": f"line-{line.id}",
                "where": statement.type_label,
                "what": line.effective_label,
                "kind": "figure",
                "source_said": line.override_source_amount,
                "source_name": ("the trial balance" if not line.formula
                                else "the lines it adds up"),
                "now_prints": line.manual_override_amount,
                "label_now": line.label_override,
                "reason": line.override_reason,
                "who": (getattr(person, "name", None)
                        or getattr(person, "email", None) or "a preparer"),
                "when": line.override_at,
                "cleared": line.manual_override_amount is None,
                "carried": False,
                "history": [],
            })
    return listed


def count_live(report):
    notes = ReportFigureOverride.query.filter_by(
        report_id=report.id, cleared_at=None).count()
    face = sum(1 for statement in
               (getattr(report.financial_year, "statements", []) or [])
               for line in statement.lines
               if line.manual_override_amount is not None)
    return notes + face


# --------------------------------------------------------------------------
# Roll-forward
# --------------------------------------------------------------------------

def carry_forward(report, prior_report):
    """OV-06. Bring last year's overrides across with their reasons.

    This year's comparative column is last year's figure, so an override
    that changed last year's figure has to travel with it - otherwise the
    comparative silently reverts to what the source said and disagrees with
    the signed accounts it is supposed to repeat.

    Carried as a record, not as a live edit on this year's figures: the
    copy is stamped `carried_from`, and it is there so the year-two
    preparer can see that the number was changed and why. Only live
    overrides travel; a withdrawn one stayed withdrawn.
    """
    if prior_report is None or report is None or report.id == prior_report.id:
        return 0

    already = {(o.section_key, o.table_index, o.row_index)
               for o in ReportFigureOverride.query.filter_by(
                   report_id=report.id).all()}
    carried = 0
    for old in ReportFigureOverride.query.filter_by(
            report_id=prior_report.id, cleared_at=None).all():
        if not old.is_live:
            continue
        key = (old.section_key, old.table_index, old.row_index)
        if key in already:
            continue
        new = ReportFigureOverride(
            report_id=report.id, section_key=old.section_key,
            table_index=old.table_index, row_index=old.row_index,
            anchor_label=old.anchor_label, para_id=old.para_id,
            source_amount=old.source_amount, source_label=old.source_label,
            source_text=old.source_text, source_name=old.source_name,
            reason=old.reason, carried_from_id=old.id,
            created_by=old.created_by, updated_by=old.updated_by,
            # Carried as history, not silently reapplied to this year's
            # figures: this year has its own trial balance.
            cleared_at=datetime.utcnow())
        db.session.add(new)
        db.session.flush()
        _event(new, "carried",
               "wording" if old.is_paragraph else "amount",
               old.source_amount if not old.is_paragraph else old.source_text,
               old.amount_override if not old.is_paragraph
               else old.text_override,
               old.reason)
        carried += 1

    if carried:
        db.session.commit()
        log.info("report %s: carried %d override(s) forward", report.id, carried)
    return carried
