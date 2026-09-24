"""Audit report builder, preview and PDF export."""
import io
import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, send_file, url_for)
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (AuditReport, AuditReportSection, Customer,
                      FinancialStatement, FinancialYear, ReportFigureOverride, StatementLine)
log = logging.getLogger(__name__)

from ..services import completion_needs
from ..services import template_follow
from ..services import overrides as overrides_service
from ..services import preparer_checks as checks_service
from ..services import provenance as provenance_service, readiness
from ..services import reports as report_service
from ..services import statements as statement_service
from ..services.audit import record

bp = Blueprint("reports", __name__, url_prefix="/reports")


# The statement helpers this blueprint's templates need - GROUP_HEADINGS,
# visible_lines, is_working_note - are registered app-wide in the factory,
# because reports/_document.html also renders from paths that never touch
# this blueprint and was failing on whichever helper it reached first.


def _assemble(report, chips=False):
    """Build the ordered list of renderable sections.

    `chips` wraps each substituted binding in an uneditable span, which the
    in-place editor needs and the delivered report must not have.
    """
    financial_year = report.financial_year
    customer = financial_year.customer

    return [report_service.section_payload(section, customer, financial_year,
                                           chips=chips)
            for section in report_service.ordered_sections(report)
            if section.is_enabled]


@bp.route("/fy/<int:fy_id>")
@login_required
def builder(fy_id):
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    # The report is built from statements that follow the APPROVED trial
    # balance, so that approval is the gate - not a second sign-off on the
    # statements themselves.
    if not financial_year.tb_is_approved:
        return render_template("reports/locked.html",
                               fy=financial_year,
                               customer=financial_year.customer)

    report = report_service.ensure_report(financial_year)

    # Last year's own sentences, into notes still holding library boilerplate.
    # Idempotent, and never touches a note the preparer has written in.
    carried = report_service.carry_forward_prior_wording(report, financial_year)
    if carried:
        flash(f"{carried} note(s) start from last year's wording — check each "
              f"one still describes the company.", "info")

    # Last year's overrides, brought across with their reasons (library
    # 3.5, OV-06): this year's comparative is last year's figure, so a
    # preparer inheriting it has to be able to see that it was changed.
    # Carried as history, never silently reapplied - this year has its own
    # trial balance.
    previous = getattr(financial_year, "previous_year", None)
    if previous is not None and previous.reports:
        overrides_service.carry_forward(report, previous.reports[0])

    # A closed engagement renders the finished document, not an editor.
    editable = not financial_year.is_closed

    gaps = report_service.content_gaps(report, financial_year)

    # The account checklist in "add a note" is scoped to whichever accounts
    # are actually behind a flagged MISSING gap, not the whole trial
    # balance - an auditor filling a gap should see a handful of relevant
    # accounts, not everything on the books. Falls back to the full list
    # only when there is no flagged gap to scope to, so the checklist isn't
    # left empty for a note that has nothing to do with a gap.
    gap_account_keys = {k for g in gaps["missing"] for k in g.get("keys", [])}
    all_accounts = report_service.mapped_accounts(financial_year)
    available_accounts = ([a for a in all_accounts if a["key"] in gap_account_keys]
                          if gap_account_keys else all_accounts)

    payloads = _assemble(report, chips=editable)
    incomplete = report_service.record_completeness(report, payloads)

    return render_template("reports/builder.html",
                           report=report, fy=financial_year,
                           look=_template_look(financial_year.customer),
                           editable=editable,
                           incomplete=incomplete,
                           needs=completion_needs.needs(incomplete),
                           follow=template_follow.panel(report),
                           summarise=completion_needs.summarise,
                           payloads=payloads,
                           ordered_sections=report_service.ordered_sections(report),
                           note_numbers=report_service.note_number_map(report),
                           content_gaps=gaps,
                           available_accounts=available_accounts,
                           attachable_notes=report_service.attachable_notes(report),
                           prior_disclosed=report_service.prior_year_disclosed(
                               financial_year),
                           prior_dropped=report_service.prior_notes_dropped(
                               report, financial_year),
                           customer=financial_year.customer,
                           final_version=financial_year.final_version,
                           tb_approved_at=financial_year.tb_approved_at,
                           readiness=readiness.check(financial_year),
                           # What is outstanding in the three places a
                           # preparer supplies what the books cannot say.
                           # Shown as counts in the header, because a link
                           # parked under fifty-three notes is a link
                           # nobody finds.
                           waiting=_waiting_counts(financial_year),
                           overrides=overrides_service.for_report(report),
                           overrides_in_force=overrides_service.count_live(report),
                           checks=checks_service.build(
                               report, financial_year, payloads),
                           pdf_available=report_service.weasyprint_available())


@bp.route("/fy/<int:fy_id>/inputs", methods=["GET", "POST"])
@login_required
def preparer_inputs(fy_id):
    """The library's own questions, put to the person who can answer them.

    Twenty-nine of them on the Preparer inputs sheet, plus the twelve
    blanks the Fields sheet ties to a paragraph and which until now
    nothing offered - an unanswered one printed "[contingent liability
    nature not provided]" in the preview with no way to provide it.

    Narrowed before it is shown: a company with no borrowings is not
    asked whether a loan payment was missed. What is left is grouped by
    who decides - what the engine concluded, what it proposes, and what
    only a person knows.

    Answering "no" is an answer and is recorded as one. Clearing puts the
    question back to unanswered, which is a different statement about the
    company and reads differently in the accounts.
    """
    from ..services import preparer_inputs as input_service

    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    if request.method == "POST":
        if request.form.get("action") == "carry":
            moved = input_service.carry_forward(
                _previous_year(financial_year), financial_year,
                user_id=current_user.id)
            flash(f"{moved} answer(s) carried from last year."
                  if moved else "Nothing to carry forward.", "success")
            return redirect(url_for("reports.preparer_inputs", fy_id=fy_id))

        saved = cleared = 0
        for item, values in _posted_inputs(request.form).items():
            if values["clear"]:
                input_service.save(financial_year, item, clear=True,
                                   commit=False)
                cleared += 1
                continue
            if not values["decided"]:
                continue
            input_service.save(
                financial_year, item, mode=values["mode"],
                answer=values["answer"], amount=values["amount"],
                source=values["source"], proposed=values["proposed"],
                accepted_proposal=values["accepted"], parts=values["parts"],
                user_id=current_user.id, commit=False)
            saved += 1
        db.session.commit()
        parts = []
        if saved:
            parts.append(f"{saved} answer(s) recorded")
        if cleared:
            parts.append(f"{cleared} put back to unanswered")
        flash(", ".join(parts) + "." if parts else "Nothing changed.",
              "success" if parts else "info")
        return redirect(url_for("reports.preparer_inputs", fy_id=fy_id))

    rows = input_service.state(financial_year)
    return render_template(
        "reports/preparer_inputs.html",
        fy=financial_year, customer=financial_year.customer,
        summary=input_service.summary(financial_year),
        derived=[r for r in rows if r["mode"] == input_service.DERIVE],
        proposed=[r for r in rows if r["mode"] == input_service.PROPOSE],
        asked=[r for r in rows if r["mode"] == input_service.ASK
               and not r["item"].startswith("field.")],
        blanks=[r for r in rows if r["item"].startswith("field.")],
        has_previous=_previous_year(financial_year) is not None)


def _waiting_counts(financial_year):
    """How much is outstanding behind each of the three side pages.

    Counted rather than merely linked: a preparer should be told there are
    eighteen unanswered questions before they go looking, not discover the
    page by accident after the draft comes out incomplete.
    """
    from ..services import document_fields, preparer_inputs, related_parties

    out = {"parties": 0, "figures": 0, "questions": 0}
    try:
        out["parties"] = len(related_parties.undecided(financial_year))
    except Exception:                      # pragma: no cover - never a 500
        log.exception("Related party count failed")
    try:
        out["figures"] = sum(len(d["missing"]) for d
                             in document_fields.documents(financial_year))
    except Exception:                      # pragma: no cover
        log.exception("Figure count failed")
    try:
        out["questions"] = len(preparer_inputs.outstanding(financial_year))
    except Exception:                      # pragma: no cover
        log.exception("Question count failed")
    return out


def _previous_year(financial_year):
    """The engagement for the year before this one, where there is one."""
    return (FinancialYear.query
            .filter(FinancialYear.customer_id == financial_year.customer_id)
            .filter(FinancialYear.end_date < financial_year.end_date)
            .order_by(FinancialYear.end_date.desc()).first())


def _posted_inputs(form):
    """Answers off the form, one entry per question.

    A question is only recorded as answered when its own "decided" box is
    set. Nothing is inferred from an empty text box: a preparer who typed
    nothing has not said "none", and a note that reads either way must
    not be told otherwise.
    """
    out = {}
    for key in form:
        if not key.startswith("decided__"):
            continue
        item = key[len("decided__"):]
        raw = (form.get(f"amount__{item}") or "").strip()
        amount = None
        if raw:
            try:
                amount = Decimal(raw.replace(",", "").replace("$", ""))
            except (InvalidOperation, ValueError):
                amount = None
        # A question that fills several rows of a note comes back as
        # several label/amount pairs. An empty pair is dropped rather than
        # stored: a blank line in a note is not an answer.
        parts = []
        index = 0
        while True:
            label = form.get(f"part_label__{item}__{index}")
            amount = form.get(f"part_amount__{item}__{index}")
            if label is None and amount is None:
                break
            label = (label or "").strip()
            amount = (amount or "").strip()
            if label or amount:
                parts.append({"label": label, "amount": amount})
            index += 1

        out[item] = {
            "parts": parts or None,
            "mode": form.get(f"mode__{item}") or "Ask",
            "decided": form.get(key) == "on" or form.get(key) == "1",
            "clear": form.get(f"clear__{item}") in ("on", "1"),
            "answer": form.get(f"answer__{item}"),
            "amount": amount,
            "source": form.get(f"source__{item}"),
            "proposed": form.get(f"proposed__{item}"),
            "accepted": form.get(f"accepted__{item}") in ("on", "1"),
        }
    return out


@bp.route("/fy/<int:fy_id>/related-parties", methods=["GET", "POST"])
@login_required
def related_parties(fy_id):
    """Who this company's related parties are, and what in the books is theirs.

    The notes library's KI-01: no parsing rule recovers the counterparty
    from a Spend Money entry, and the test client's director appears under
    four spellings, so the list comes from the preparer and every match is
    shown back before it counts. Nothing on this page decides anything by
    itself - it suggests, and a person confirms or rejects.
    """
    from ..services import related_parties as rp_service

    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "add":
                rp_service.add(financial_year,
                               request.form.get("name"),
                               kind=request.form.get("kind") or "director",
                               spellings=request.form.get("spellings"),
                               note=request.form.get("note"))
                flash("Related party added.", "success")
            elif action == "update":
                party = db.session.get(rp_service.RelatedParty,
                                       int(request.form.get("party_id", 0)))
                if party and party.financial_year_id == fy_id:
                    rp_service.update(party,
                                      name=request.form.get("name"),
                                      kind=request.form.get("kind"),
                                      spellings=request.form.get("spellings"),
                                      note=request.form.get("note"))
                    flash("Related party updated.", "success")
            elif action == "remove":
                party = db.session.get(rp_service.RelatedParty,
                                       int(request.form.get("party_id", 0)))
                if party and party.financial_year_id == fy_id:
                    rp_service.remove(party)
                    flash("Related party removed, with its confirmed matches.",
                          "success")
            elif action == "decide":
                decided = _record_decisions(financial_year, request.form,
                                            rp_service)
                if decided:
                    flash(f"{decided} decision(s) recorded.", "success")
            elif action == "reopen":
                rp_service.unsettle(financial_year, "tb_account",
                                    int(request.form.get("subject_id", 0)))
                flash("Put back for a decision.", "success")
            elif action == "add_directors":
                added = rp_service.add_directors(
                    financial_year, rp_service.missing_directors(financial_year))
                if added:
                    flash(f"{len(added)} director(s) added from the client "
                          f"record. Add spellings and confirm what is "
                          f"theirs below.", "success")
                else:
                    flash("Nothing to add - already on the list.", "info")
        except ValueError as bad:
            flash(str(bad), "error")
        return redirect(url_for("reports.related_parties", fy_id=fy_id))

    rp_service.carry_forward(financial_year)
    return render_template("reports/related_parties.html",
                           fy=financial_year,
                           customer=financial_year.customer,
                           kinds=rp_service.KINDS,
                           parties=rp_service.register(financial_year),
                           candidates=rp_service.candidates(financial_year),
                           state=rp_service.state(financial_year),
                           missing_directors=rp_service.missing_directors(
                               financial_year))


def _record_decisions(financial_year, form, rp_service):
    """One row of the matching table per decision the preparer made.

    A row left on "not decided yet" writes nothing. Silence on this page
    is the same as silence anywhere else in AuditMate: it holds the note
    rather than being read as an answer.
    """
    decided = 0
    for key in form:
        if not key.startswith("decide__"):
            continue
        choice = (form.get(key) or "").strip()
        if not choice:
            continue                                   # not decided yet
        subject_id = int(key[len("decide__"):])
        party_id = None if choice == "not-related" else int(choice)
        rp_service.decide(financial_year, "tb_account", subject_id,
                          party_id=party_id,
                          subject_label=form.get(f"label__{subject_id}"),
                          matched_on=form.get(f"matched__{subject_id}"))
        decided += 1
    return decided


@bp.route("/api/section/<int:section_id>", methods=["PATCH"])
@login_required
def update_section(section_id):
    """Toggle a section on/off, retitle it, or save its text."""
    section = db.session.get(AuditReportSection, section_id) or abort(404)
    payload = request.get_json(silent=True) or {}

    if "is_enabled" in payload:
        section.is_enabled = bool(payload["is_enabled"])
    if "title" in payload:
        section.title = (payload["title"] or section.title).strip()
    if "content_html" in payload:
        section.content_html = payload["content_html"]
    if "sort_order" in payload:
        section.sort_order = int(payload["sort_order"])

    if "labels" in payload:
        # Fixed captions on the cover page - "CORPORATE INFORMATION" and the
        # like. Merged rather than replaced so editing one caption cannot
        # discard the others, and a caption typed back to its default is
        # dropped rather than stored as a redundant override.
        binding = dict(section.data_binding or {})
        labels = dict(binding.get("labels") or {})
        for key, value in (payload["labels"] or {}).items():
            text = (value or "").strip()
            if text:
                labels[key] = text
            else:
                labels.pop(key, None)
        binding["labels"] = labels
        section.data_binding = binding

    record("report_section", section.id, "update",
           after={"enabled": section.is_enabled})
    db.session.commit()

    return jsonify({"ok": True})


_COVER_FIELDS = {"legal_name", "uen", "directors", "company_secretary", "office"}


@bp.route("/api/section/<int:section_id>/cover-field", methods=["PATCH"])
@login_required
def update_cover_field(section_id):
    """Edit a company fact printed on the cover page.

    The company's name, registration number, directors, secretary and
    registered office belong to the customer record, not to the report: typed
    over on the page they would print here and nowhere else, and next year's
    report would ask again. So the cover writes them back to the record.
    """
    section = db.session.get(AuditReportSection, section_id) or abort(404)
    payload = request.get_json(silent=True) or {}
    field = payload.get("field")
    if field not in _COVER_FIELDS:
        abort(400)
    text = (payload.get("value") or "").replace("\r", "")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    report = db.session.get(AuditReport, section.report_id) or abort(404)
    year = db.session.get(FinancialYear, report.financial_year_id) or abort(404)
    customer = db.session.get(Customer, year.customer_id) or abort(404)
    before = {k: getattr(customer, k, None) for k in
              ("legal_name", "uen", "directors", "company_secretary",
               "address_line1", "address_line2", "postal_code")}

    if field == "office":
        postal = ""
        if lines:
            match = re.match(r"^(?:singapore\s+)?(\d{6})$", lines[-1], re.I)
            if match:
                postal = match.group(1)
                lines = lines[:-1]
        customer.address_line1 = lines[0] if lines else ""
        customer.address_line2 = ", ".join(lines[1:]) if len(lines) > 1 else ""
        customer.postal_code = postal
    elif field == "directors":
        customer.directors = "\n".join(lines)
    else:
        setattr(customer, field, " ".join(lines))

    record("customer", customer.id, "update",
           before=before, after={"cover_field": field})
    db.session.commit()
    return jsonify({"ok": True})


def _decimal(raw):
    """Parse a figure typed into the report. Returns (value, error).

    One parser for both places a figure can be typed - a line on the face
    and a row in a note - so "(1,234)" cannot mean one thing on the balance
    sheet and another two pages later.
    """
    return overrides_service.parse_amount(raw)


@bp.route("/api/line/<int:line_id>", methods=["PATCH"])
@login_required
def update_line(line_id):
    """Edit a statement line from inside the report.

    The wording and the figure are handled differently on purpose. A label is
    presentation - one client says Revenue, another Turnover - so it is simply
    stored. A figure is not: it is written through the same override path the
    Statements page uses, so the computed value is kept underneath, the change
    is recorded, and every dependent total is recalculated. Typing a number
    into the report can therefore never leave the report disagreeing with the
    statements behind it.
    """
    line = db.session.get(StatementLine, line_id) or abort(404)
    payload = request.get_json(silent=True) or {}
    before = {"label": line.effective_label, "amount": str(line.effective_amount)}

    if "label" in payload:
        text = (payload["label"] or "").strip()
        # Back to the template wording when cleared or typed back to it.
        line.label_override = None if (not text or text == line.label) else text

    if "amount" in payload:
        value, error = _decimal(payload["amount"])
        if error:
            return jsonify({"ok": False, "error": error}), 400
        # A figure on the face is overridden on the same terms as a figure
        # in a note (library 3.5, OV-04): a reason, entered at the time.
        # Clearing needs none - it withdraws a change already on the record.
        if value is not None and value != line.manual_override_amount:
            reason = " ".join(str(payload.get("reason") or "").split())
            if not reason:
                return jsonify({
                    "ok": False, "needs_reason": True,
                    "error": "Say why this figure is being changed. The reason "
                             "is kept with the override and shown to whoever "
                             "reviews the draft."}), 400
            if line.override_source_amount is None:
                line.override_source_amount = line.effective_amount
            line.override_reason = reason
            line.override_at = datetime.utcnow()
            line.override_by = current_user.id
        elif value is None:
            line.override_at = datetime.utcnow()
            line.override_by = current_user.id
        line.manual_override_amount = value
        line.source = ("manual" if value is not None
                       else ("computed" if line.formula else "auto"))

    record("statement_line", line.id, "report_edit", before=before,
           after={"label": line.effective_label,
                  "amount": str(line.effective_amount)})
    db.session.commit()

    # Totals are formulas over these lines, so the whole statement is redone.
    statement_service.recalculate(line.statement_id)

    statement = line.statement
    return jsonify({
        "ok": True,
        "lines": [{"id": l.id,
                   "amount": float(l.effective_amount or 0),
                   "label": l.effective_label,
                   "overridden": l.is_overridden,
                   "label_overridden": l.label_is_overridden}
                  for l in statement.lines],
    })


@bp.route("/api/note-row", methods=["PATCH"])
@login_required
def update_note_row():
    """Type over one row of a note table, with the reason recorded.

    Note tables have no stored rows - they are recomputed from the trial
    balance on every render - so the edit is held by position and reapplied
    at render time. `anchor_label` records what the row said when it was
    edited, so a later rebuild that changes the note shows the edit as stale
    instead of moving it onto a different account.

    Library 3.5 (OV-01, OV-04) makes every figure overridable and the reason
    compulsory, so a save without one is refused. What the source said is
    read back here from the freshly built table rather than taken from the
    browser: the original is the one thing in the record the person typing
    over it must not be able to set.

    Clearing the cell does not delete the record (OV-07) - the source figure
    comes back and the history stays.
    """
    payload = request.get_json(silent=True) or {}
    section = db.session.get(AuditReportSection,
                             int(payload.get("section_id", 0))) or abort(404)

    table_index = int(payload.get("table_index", 0))
    row_index = int(payload.get("row_index", 0))
    source = _source_row(section, table_index, row_index)

    fields = {}
    if "label" in payload:
        fields["label"] = (payload["label"] or "").strip() or None
    if "amount" in payload:
        value, error = overrides_service.parse_amount(payload["amount"])
        if error:
            return jsonify({"ok": False, "error": error}), 400
        fields["amount"] = value

    # Typing the source figure back in, or emptying the cell, is a revert -
    # not a new override that happens to match.
    existing = overrides_service.for_row(section, table_index, row_index)
    reverting = (
        "amount" in fields
        and (fields["amount"] is None
             or (source.get("current") is not None
                 and fields["amount"] == source["current"]))
        and fields.get("label", ...) in (..., None, source.get("label")))
    if reverting and existing is not None and existing.is_live:
        overrides_service.clear(existing)
        record("report_figure_override", existing.id, "note_edit_cleared",
               after={"section": section.section_key})
        db.session.commit()
        return jsonify({"ok": True, "cleared": True,
                        "amount": _float(source.get("current"))})
    if reverting and existing is None:
        return jsonify({"ok": True, "cleared": True,
                        "amount": _float(source.get("current"))})

    try:
        override = overrides_service.set_figure(
            section, table_index, row_index,
            reason=payload.get("reason"),
            label=fields.get("label", ...),
            amount=fields.get("amount", ...),
            anchor_label=payload.get("anchor_label") or source.get("label"),
            source_amount=source.get("current"),
            source_label=source.get("label"),
            source_name=source.get("source_name"))
    except overrides_service.ReasonRequired as needed:
        return jsonify({"ok": False, "error": str(needed),
                        "needs_reason": True}), 400

    if override is None:
        return jsonify({"ok": True, "cleared": True})

    record("report_figure_override", override.id, "note_edit",
           before={"source_amount": str(override.source_amount)},
           after={"section": section.section_key,
                  "label": override.label_override,
                  "amount": str(override.amount_override),
                  "reason": override.reason})
    db.session.commit()
    return jsonify({"ok": True, "override_id": override.id,
                    "source_amount": _float(override.source_amount),
                    "reason": override.reason})


def _float(value):
    return None if value is None else float(value)


def _source_row(section, table_index, row_index):
    """The row as its source gives it, before any override is laid over it.

    Built here rather than trusted from the browser. The figure the source
    gave is the whole point of the record (OV-03); a value posted by the
    page doing the overriding could say anything.
    """
    from ..services import notes as notes_service

    spec = (report_service._spec_index().get(section.section_key, {})
            .get("note_table"))
    if spec is None and section.data_binding:
        spec = section.data_binding.get("note_table_specs")
    tables = notes_service.build_tables(spec, section.report.financial_year)
    try:
        row = tables[table_index]["rows"][row_index]
    except (IndexError, KeyError, TypeError):
        return {}
    return {"label": row.get("label"), "current": row.get("current"),
            "source_name": ("the trial balance" if row.get("ref")
                            else "the notes library")}


@bp.route("/api/document-figure", methods=["PATCH"])
@login_required
def update_document_figure():
    """Answer an Incomplete cell in a note, without leaving the report.

    NOT an override. An override says a source gave one figure and the
    accounts print another - it needs a reason because it contradicts
    something. There is nothing to contradict here: the cell is
    Incomplete because nobody has answered yet, so this supplies the
    answer the same way the Figures page would.

    It writes through document_fields.save() - the exact function the
    Figures page calls - so a figure typed from this cell and a figure
    typed from that page are the same act on the same row. Answering it
    here clears it there, and answering it there clears it here; neither
    can say something the other does not.

    Only reachable for a hold the binding engine marked editable, which
    means it carries a token and field document_fields.save() defines -
    see bindings.Held.editable. A hold with no token (an unanswered
    preparer question, a structural gap) has nothing to route to and is
    never offered this control.
    """
    from ..services import document_fields

    payload = request.get_json(silent=True) or {}
    fy_id = payload.get("financial_year_id")
    financial_year = db.session.get(FinancialYear, fy_id) if fy_id else None
    if financial_year is None:
        return jsonify({"ok": False, "error": "Unknown engagement."}), 404

    if financial_year.is_closed:
        return jsonify({"ok": False,
                        "error": "This engagement is closed."}), 400

    token = (payload.get("token") or "").strip()
    field = (payload.get("field") or "").strip()
    if not token or not field:
        # Never trusted from the page alone - a row with no editable hold
        # has no token to post, so a request without one is either a bug
        # or a cell that should never have been offered as editable.
        return jsonify({"ok": False, "error": "Nothing to save here."}), 400

    scope = (payload.get("scope") or "").strip()
    member = (payload.get("member") or "").strip()
    raw = payload.get("amount")

    if raw is None or str(raw).strip() == "":
        document_fields.save(financial_year, token, field, scope=scope,
                             member=member, clear=True)
        db.session.commit()
        if token == "PRIORFS":
            from ..services import statements as statements_service

            statements_service.build_all(financial_year.id, use_ai=False)
        return jsonify({"ok": True, "cleared": True})

    cleaned = str(raw).replace(",", "").replace("$", "").strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1].strip()
    try:
        amount = Decimal(cleaned)
        if negative:
            amount = -amount
    except InvalidOperation:
        return jsonify({"ok": False,
                        "error": f"{raw!r} is not a number."}), 400

    document_fields.save(financial_year, token, field, scope=scope,
                         member=member, amount=amount,
                         found_at="entered from the report")
    db.session.commit()

    # PRIORFS answers a comparative, and a statement line's amount_previous
    # is a stored figure, not one resolved live at render time the way a
    # note's binding is - typing it here does nothing to the page until
    # the statements are rebuilt. build_all() also cascades to any LATER
    # engagement whose own comparative was taken from this one, so a
    # correction here does not leave next year quoting the old figure.
    if token == "PRIORFS":
        from ..services import statements as statements_service

        statements_service.build_all(financial_year.id, use_ai=False)

    return jsonify({"ok": True, "amount": float(amount)})


@bp.route("/api/follow-template", methods=["PATCH"])
@login_required
def follow_template():
    """Turn "follow the template's disclosures" on or off, or put back one
    table it left out. Kept per engagement, so a rebuilt report keeps it."""
    from ..services import document_fields

    payload = request.get_json(silent=True) or {}
    year = db.session.get(FinancialYear, payload.get("financial_year_id") or 0)
    if year is None:
        return jsonify({"ok": False, "error": "Unknown engagement."}), 404
    if year.is_closed:
        return jsonify({"ok": False, "error": "This engagement is closed."}), 400

    action = (payload.get("action") or "").strip()
    if action == "add_back" and payload.get("table_id"):
        document_fields.save(year, template_follow.ADDBACK_TOKEN,
                             str(payload["table_id"]), text="yes",
                             found_at="put back in the report")
    elif action in ("on", "off"):
        if action == "off":
            document_fields.save(year, template_follow.FOLLOW_TOKEN, "template",
                                 text="off", found_at="set in the report")
        else:
            document_fields.save(year, template_follow.FOLLOW_TOKEN, "template",
                                 clear=True)
    else:
        return jsonify({"ok": False, "error": "Nothing to do."}), 400
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/api/confirm-paragraph", methods=["PATCH"])
@login_required
def confirm_paragraph():
    """Answer a paragraph the library holds for the preparer: it applies, or
    it does not.

    The library marks some paragraphs "Preparer confirms ..." - the Company
    renders services, a material uncertainty exists - that no balance or
    document can decide. They were listed as CONFIRM with nowhere to confirm,
    so their notes stayed incomplete for good. A yes prints the paragraph in
    the note; a no leaves it out. Either way the answer is kept for the
    engagement (a CONFIRM figure), so a rebuilt note does not ask again.
    """
    from ..services import document_fields

    payload = request.get_json(silent=True) or {}
    section = db.session.get(AuditReportSection, payload.get("section_id") or 0)
    if section is None:
        return jsonify({"ok": False, "error": "Unknown note."}), 404
    report = db.session.get(AuditReport, section.report_id)
    year = db.session.get(FinancialYear, report.financial_year_id)
    if year.is_closed:
        return jsonify({"ok": False, "error": "This engagement is closed."}), 400

    para_id = (payload.get("para_id") or "").strip()
    decision = (payload.get("decision") or "").strip().lower()
    if not para_id or decision not in ("yes", "no"):
        return jsonify({"ok": False, "error": "Nothing to save here."}), 400

    binding = dict(section.data_binding or {})
    wording = ""
    for kind in ("awaiting_preparer", "offered_to_preparer"):
        kept = []
        for item in binding.get(kind, []):
            if item.get("para_id") == para_id:
                wording = wording or (item.get("wording") or "")
            else:
                kept.append(item)
        if kept or kind in binding:
            binding[kind] = kept

    if decision == "yes" and wording.strip() and "[table" not in wording:
        section.content_html = ((section.content_html or "")
                                + f'<p data-para="{para_id}">{wording}</p>')
    section.data_binding = binding
    document_fields.save(year, "CONFIRM", para_id, text=decision,
                         found_at="confirmed in the report")
    db.session.commit()
    return jsonify({"ok": True, "decision": decision})


@bp.route("/api/note-paragraph", methods=["PATCH"])
@login_required
def update_note_paragraph():
    """Type over one paragraph of a note, with the reason recorded.

    Library 3.5, OV-02: a client circumstance the library did not
    anticipate is corrected in the sentence, not by dropping the note. The
    new wording goes into the section's stored text the way every other
    edit does; what this adds is the record beside it, so the reviewer can
    see that a sentence in the accounts is not the library's own.

    The browser saves the paragraph first and the section body second, so
    the id returned here can be written into the paragraph as
    `data-override`, which is what keeps the record with its sentence when
    the note is reordered.
    """
    payload = request.get_json(silent=True) or {}
    section = db.session.get(AuditReportSection,
                             int(payload.get("section_id", 0))) or abort(404)

    wording = payload.get("wording") or ""
    try:
        override = overrides_service.set_paragraph(
            section, wording=wording,
            source_text=payload.get("source_text") or "",
            reason=payload.get("reason"),
            para_id=(payload.get("para_id") or None),
            override_id=payload.get("override_id"))
    except overrides_service.ReasonRequired as needed:
        return jsonify({"ok": False, "error": str(needed),
                        "needs_reason": True}), 400

    if override is None:
        return jsonify({"ok": True, "unchanged": True})

    record("report_figure_override", override.id, "note_wording",
           after={"section": section.section_key,
                  "para": override.para_id,
                  "reason": override.reason})
    db.session.commit()
    return jsonify({"ok": True, "override_id": override.id})


@bp.route("/api/override/<int:override_id>/clear", methods=["POST"])
@login_required
def clear_override(override_id):
    """Withdraw an override. OV-07: the source comes back, the record stays."""
    override = db.session.get(ReportFigureOverride, override_id) or abort(404)
    source = override.source_text
    try:
        overrides_service.clear(override,
                               (request.get_json(silent=True) or {}).get("reason"))
    except overrides_service.ReasonRequired as needed:
        return jsonify({"ok": False, "error": str(needed),
                        "needs_reason": True}), 400
    record("report_figure_override", override.id, "cleared",
           after={"section": override.section_key})
    db.session.commit()
    return jsonify({"ok": True, "restores": source})


@bp.route("/api/line/<int:line_id>/sources")
@login_required
def line_sources(line_id):
    """Which trial balance accounts make up this printed figure."""
    line = db.session.get(StatementLine, line_id) or abort(404)
    return jsonify({"ok": True, **provenance_service.for_statement_line(line)})


@bp.route("/api/account/<int:account_id>/sources")
@login_required
def account_sources(account_id):
    """Provenance for a note-table row that came from one account."""
    found = provenance_service.for_account(account_id)
    if found is None:
        abort(404)
    return jsonify({"ok": True, **found})


@bp.route("/api/fy/<int:fy_id>/coverage")
@login_required
def coverage(fy_id):
    """What in the trial balance never reached the report."""
    return jsonify(provenance_service.coverage(fy_id))


@bp.route("/api/fy/<int:fy_id>/suggest", methods=["POST"])
@login_required
def suggest(fy_id):
    """Propose a home for every account the report is missing.

    Mapping rules run first; only what they cannot place is sent to the AI,
    and only account names go - never figures, never the client's name.
    """
    use_ai = bool((request.get_json(silent=True) or {}).get("use_ai", True))
    return jsonify(provenance_service.suggest(fy_id, use_ai=use_ai))


# Sections created here rather than from the section library. The key is
# prefixed so a custom note can always be told apart from a template one -
# it has no spec, so it must never be looked up in the library, and it is
# the only kind of section that may be deleted.
CUSTOM_PREFIX = "custom_"


@bp.route("/api/report/<int:report_id>/section", methods=["POST"])
@login_required
def add_section(report_id):
    """Add a note that is not in the library.

    The firm's point 6: *"Inside the report the preparer can edit any note,
    change any figure, or add a new note that is not in the list."* The first
    two were built; this is the third.

    A statutory set of accounts regularly needs a note no template
    anticipated - a subsequent event, a related party transaction, a
    contingent liability. Without this the preparer generates the Word file
    and types the note into it by hand, which means the app's copy and the
    delivered copy have said different things ever since.
    """
    report = db.session.get(AuditReport, report_id) or abort(404)
    # The engagement, not report.status. Exporting the Word file sets the
    # report final as a side effect, while the builder keeps offering its
    # editing controls because its own gate is whether the engagement is
    # closed. Gating on report.status here meant the buttons were on screen
    # and the endpoint behind them said no.
    if report.financial_year.is_closed:
        return jsonify({"ok": False,
                        "error": "This engagement is closed. Reopen it to "
                                 "add a note."}), 400

    payload = request.get_json(silent=True) or {}
    title = (payload.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "Give the note a title."}), 400
    title = title[:255]

    # Where it sits: standing alone at the end (unchanged default), or
    # attached under an existing note as a sub-item - "11.1" rather than a
    # bare number at the bottom, when the auditor is filling a gap that
    # belongs beside a specific note rather than writing something new.
    parent = None
    parent_id = payload.get("parent_section_id")
    if parent_id:
        parent = db.session.get(AuditReportSection, int(parent_id))
        if not parent or parent.report_id != report.id or not parent.is_enabled:
            return jsonify({"ok": False,
                            "error": "That note isn't available to attach to."}), 400

    # The key is always unique; the TITLE was not checked at all, so adding
    # the same note twice produced two sections that print identically and
    # renumber everything after them. A note already in the report - whether
    # it came from the library or was added here - is the one to edit.
    clash = next((s for s in report.sections
                  if (s.title or "").strip().lower() == title.lower()), None)
    if clash is not None:
        return jsonify({
            "ok": False,
            "error": (f"“{clash.title}” is already in this report"
                      f"{'' if clash.is_enabled else ' (switched off)'}. "
                      f"Edit that note rather than adding a second one."),
        }), 400

    existing = {s.section_key for s in report.sections}
    index = 1
    while f"{CUSTOM_PREFIX}{index}" in existing:
        index += 1

    if parent:
        sort_order = max((c.sort_order or 0 for c in parent.children), default=0) + 1
    else:
        # Placed last by default, which for a set of accounts means after
        # the existing notes and before nothing - the preparer drags it
        # where it belongs, the same as every other section.
        sort_order = (max((s.sort_order or 0) for s in report.sections)
                     if report.sections else 0) + 1

    # Real figures instead of hand-typed ones: whichever trial balance
    # lines the auditor ticked become an actual table, built the same way
    # every catalogue note's table already is - traceable to source, not a
    # number retyped into free text.
    account_keys = [k for k in (payload.get("account_keys") or [])
                    if isinstance(k, str)]
    valid_keys = {a["key"] for a in report_service.mapped_accounts(
        report.financial_year)}
    account_keys = [k for k in account_keys if k in valid_keys]
    data_binding = None
    if account_keys:
        data_binding = {"note_table_specs": [
            {"source": "accounts", "keys": account_keys, "heading": "", "total": ""}
        ]}

    section = AuditReportSection(
        report_id=report.id,
        section_key=f"{CUSTOM_PREFIX}{index}",
        title=title,
        section_type="free_text",
        sort_order=sort_order,
        is_enabled=True,
        parent_section_id=parent.id if parent else None,
        # Deliberately not empty. An empty note renders as a heading with
        # nothing under it and looks like a fault; a visible prompt says the
        # note is waiting to be written. Reported as a gap for as long as it
        # stands, so it cannot quietly reach a client - see content_gaps().
        content_html=report_service.UNWRITTEN_NOTE_HTML,
        data_binding=data_binding,
    )
    db.session.add(section)
    db.session.flush()

    # "Add to the library": the same note, written once, proposed to every
    # future engagement from then on - not retyped each time the same gap
    # is flagged. Manual by default; nothing an auditor writes for one
    # client's specific situation should silently start appearing on every
    # other client's report without a person choosing to include it.
    library_note = None
    if payload.get("save_scope") == "library":
        from ..models import NoteLibraryEntry
        base_key = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or "note"
        key = base_key
        i = 2
        while NoteLibraryEntry.query.filter_by(key=key).first():
            key = f"{base_key}_{i}"
            i += 1

        piece = {
            "ref": f"CUSTOM-{key}",
            "output_form": "Table" if account_keys else "Narrative paragraph",
            "tick_state": "manual",
            "tb_keys": account_keys,
            "wording": "",
            "build_note": "Added by an auditor through the report builder, "
                          "not the FRS spreadsheet.",
            "requirement": "",
        }
        library_note = NoteLibraryEntry(
            key=key, heading=title, tick_state="manual", sort_order=999,
            trigger_keys=None, pieces=[piece], subsections=[],
            source="auditor_added", added_by=current_user.id,
            added_reason=(f"Added while preparing "
                          f"{report.financial_year.customer.name} "
                          f"{report.financial_year.year_label}."),
        )
        db.session.add(library_note)

    record("report_section", None, "add",
           after={"report": report.id, "title": title,
                 "parent_section_id": parent.id if parent else None,
                 "account_keys": account_keys,
                 "saved_to_library": bool(library_note)})
    db.session.commit()

    return jsonify({"ok": True, "section_id": section.id,
                    "library_key": library_note.key if library_note else None})


@bp.route("/api/section/<int:section_id>", methods=["DELETE"])
@login_required
def delete_section(section_id):
    """Remove a note that was added here.

    Only a custom one. A template section is switched off rather than
    deleted - it belongs to the library, and deleting it would leave the
    report unable to say what it is missing.
    """
    section = db.session.get(AuditReportSection, section_id) or abort(404)
    if not (section.section_key or "").startswith(CUSTOM_PREFIX):
        return jsonify({"ok": False,
                        "error": "That is a library section. Switch it off "
                                 "instead of deleting it."}), 400
    if section.report.financial_year.is_closed:
        return jsonify({"ok": False,
                        "error": "This engagement is closed. Reopen it "
                                 "first."}), 400
    if section.children:
        return jsonify({"ok": False,
                        "error": "This note has its own sub-note attached "
                                 "(\"" + section.children[0].title + "\"). "
                                 "Delete that first."}), 400

    ReportFigureOverride.query.filter_by(
        report_id=section.report_id,
        section_key=section.section_key).delete(synchronize_session=False)

    # Don't leave a statement line pointing at a note that no longer
    # exists - it printed a number before, it must print nothing now.
    linked_lines = (StatementLine.query
                    .join(FinancialStatement)
                    .filter(FinancialStatement.financial_year_id == section.report.financial_year_id)
                    .filter(StatementLine.note_ref == section.section_key)
                    .all())
    for line in linked_lines:
        line.note_ref = None

    record("report_section", section.id, "delete",
           before={"title": section.title})
    db.session.delete(section)
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/api/report/<int:report_id>/reorder", methods=["POST"])
@login_required
def reorder(report_id):
    """Persist a drag-and-drop reorder of the sections."""
    report = db.session.get(AuditReport, report_id) or abort(404)
    order = (request.get_json(silent=True) or {}).get("order", [])

    lookup = {s.id: s for s in report.sections}
    for position, section_id in enumerate(order):
        section = lookup.get(int(section_id))
        if section:
            section.sort_order = position

    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/fy/<int:fy_id>/finalise", methods=["POST"])
@login_required
def finalise(fy_id):
    """Sign the report off and close the engagement.

    This is the last act on a file: the report is issued, so the engagement
    stops being work in progress. It locks the report against further
    editing for the same reason the trial balance locks on approval - a
    document that has been issued and then quietly edited is worse than no
    version control at all.

    Reversible by design (see `reopen`). An engagement closed by mistake
    should not need someone in the database.
    """
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    if financial_year.is_closed:
        flash("This engagement is already closed.", "warning")
        return redirect(url_for("reports.builder", fy_id=fy_id))

    if not financial_year.tb_is_approved:
        flash("Approve the trial balance first - the report is built from "
              "it, so it cannot be signed off before the figures are.",
              "error")
        return redirect(url_for("trial_balance.index", fy_id=fy_id))

    report = financial_year.report
    if report is None:
        flash("Generate the audit report before closing the engagement.",
              "error")
        return redirect(url_for("reports.builder", fy_id=fy_id))

    # No clean final copy while anything is incomplete.
    incomplete = report_service.record_completeness(report, _assemble(report))
    if incomplete:
        # "Item" rather than "note": a statement that does not reconcile
        # blocks approval too, and calling that a note sends the preparer
        # looking through the notes for something that is not there.
        flash(f"{len(incomplete)} item(s) are still incomplete, so the "
              f"accounts cannot be approved yet: "
              f"{', '.join(title for title, _reasons in incomplete)}. "
              f"Each is marked in the report, with what it is waiting for.",
              "error")
        return redirect(url_for("reports.builder", fy_id=fy_id))

    note = (request.form.get("note") or "").strip() or None

    report.status = "final"
    if report.generated_at is None:
        report.generated_at = datetime.utcnow()
        report.generated_by = current_user.id

    financial_year.status = "closed"
    financial_year.closed_at = datetime.utcnow()
    financial_year.closed_by = current_user.id
    financial_year.closed_note = note

    record("financial_year", financial_year.id, "close",
           after={"status": "closed", "note": note}, commit=True)

    flash(f"{financial_year.customer.name} {financial_year.year_label} is "
          f"closed. The report is locked; reopen it if it needs changing.",
          "success")
    return redirect(url_for("reports.builder", fy_id=fy_id))


@bp.route("/fy/<int:fy_id>/regenerate", methods=["POST"])
@login_required
def regenerate(fy_id):
    """Rebuild this year's report from the current statements and notes.

    For a report made before a fix, a new trial balance or a change of the
    customer's template. Refused for a report that has been issued as final
    and for a closed engagement: those are the record of what was delivered.
    """
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    if not financial_year.tb_is_approved:
        flash("Approve the trial balance first.", "error")
        return redirect(url_for("reports.builder", fy_id=fy_id))
    if financial_year.is_closed:
        flash("This engagement is closed. Reopen it before regenerating "
              "the report.", "error")
        return redirect(url_for("reports.builder", fy_id=fy_id))

    existing = AuditReport.query.filter_by(financial_year_id=fy_id).first()
    if existing is not None and existing.status == "final":
        flash("This report was issued as final. Regenerating would replace "
              "the delivered copy, so it is not offered.", "error")
        return redirect(url_for("reports.builder", fy_id=fy_id))

    report = report_service.regenerate_report(financial_year)
    record("audit_report", report.id, "regenerate",
           after={"replaced": existing.id if existing else None},
           commit=True)

    flash("The report was rebuilt from the current statements and notes. "
          "Anything typed into the old report was replaced.", "success")
    return redirect(url_for("reports.builder", fy_id=fy_id))


@bp.route("/fy/<int:fy_id>/reopen", methods=["POST"])
@login_required
def reopen(fy_id):
    """Put a closed engagement back into work."""
    financial_year = db.session.get(FinancialYear, fy_id) or abort(404)

    if not financial_year.is_closed:
        flash("That engagement is not closed.", "warning")
        return redirect(url_for("reports.builder", fy_id=fy_id))

    financial_year.status = "report_generated"
    financial_year.closed_at = None
    financial_year.closed_by = None
    financial_year.closed_note = None

    record("financial_year", financial_year.id, "reopen",
           after={"status": "report_generated"}, commit=True)

    flash("Engagement reopened. The report is editable again.", "success")
    return redirect(url_for("reports.builder", fy_id=fy_id))


def _template_look(customer):
    """The customer's own report layout, or None for the standard one.

    Handed to the preview, PDF and Word export alike - see
    docx_export.template_look.
    """
    from ..services import docx_export
    return docx_export.template_look(customer.report_template_path)


@bp.route("/<int:report_id>/preview")
@login_required
def preview(report_id):
    report = db.session.get(AuditReport, report_id) or abort(404)
    payloads = _assemble(report)
    incomplete = report_service.record_completeness(report, payloads)

    return render_template("reports/preview.html",
                           report=report,
                           fy=report.financial_year,
                           customer=report.financial_year.customer,
                           payloads=payloads,
                           draft_incomplete=bool(incomplete),
                           note_numbers=report_service.note_number_map(report),
                           checks=checks_service.build(
                               report, report.financial_year, payloads),
                           look=_template_look(report.financial_year.customer),
                           for_pdf=False)


@bp.route("/<int:report_id>/export/word")
@login_required
def export_word(report_id):
    """The unaudited financial statements as an editable Word document.

    This is the deliverable. The firm finishes it by hand - changing a
    figure, rewriting a note, adding one that was never in the list - which
    is exactly why it is a .docx and not a PDF.

    It converts the same HTML the preview renders, so what is on screen and
    what lands in Word cannot drift apart.
    """
    from ..services import docx_export

    report = db.session.get(AuditReport, report_id) or abort(404)
    financial_year = report.financial_year
    payloads = _assemble(report)
    incomplete = report_service.record_completeness(report, payloads)

    html = render_template("reports/preview.html",
                           report=report,
                           fy=financial_year,
                           customer=financial_year.customer,
                           payloads=payloads,
                           draft_incomplete=bool(incomplete),
                           note_numbers=report_service.note_number_map(report),
                           checks=checks_service.build(
                               report, financial_year, payloads),
                           look=_template_look(report.financial_year.customer),
                           for_pdf=True)

    try:
        # Use customer's custom template if they uploaded one
        template_path = financial_year.customer.report_template_path
        data = docx_export.build(html, draft=bool(incomplete), template_path=template_path)
    except Exception as exc:                        # noqa: BLE001
        flash(f"Word export failed: {exc}", "error")
        return redirect(url_for("reports.preview", report_id=report.id))

    if incomplete:
        # A copy to review, stamped on every page. Not the final accounts.
        record("audit_report", report.id, "export_word_draft",
               after={"incomplete_notes": len(incomplete)}, commit=True)
    else:
        report.status = "final"
        report.generated_at = datetime.utcnow()
        report.generated_by = current_user.id
        if financial_year.status in ("in_progress", "statements_shared", "approved"):
            financial_year.status = "report_generated"
        record("audit_report", report.id, "export_word")
        db.session.commit()

    filename = (f"{financial_year.customer.name}_{financial_year.year_label}"
                f"_Unaudited_Financial_Statements"
                f"{'_DRAFT_INCOMPLETE' if incomplete else ''}.docx").replace(" ", "_")

    return send_file(
        io.BytesIO(data),
        mimetype=("application/vnd.openxmlformats-officedocument"
                  ".wordprocessingml.document"),
        as_attachment=True, download_name=filename)


@bp.route("/<int:report_id>/export")
@login_required
def export(report_id):
    """Export to PDF, or fall back to the printable page."""
    report = db.session.get(AuditReport, report_id) or abort(404)
    payloads = _assemble(report)
    incomplete = report_service.record_completeness(report, payloads)

    html = render_template("reports/preview.html",
                           report=report,
                           fy=report.financial_year,
                           customer=report.financial_year.customer,
                           payloads=payloads,
                           draft_incomplete=bool(incomplete),
                           note_numbers=report_service.note_number_map(report),
                           checks=checks_service.build(
                               report, report.financial_year, payloads),
                           look=_template_look(report.financial_year.customer),
                           for_pdf=True)

    if not report_service.weasyprint_available():
        # WeasyPrint isn't installed (typical on Windows dev machines) — send
        # the user to the printable page instead of failing.
        flash("PDF engine not installed — use your browser's Print → Save as "
              "PDF on this page. (Install WeasyPrint on the server for "
              "one-click export.)", "warning")
        return redirect(url_for("reports.preview", report_id=report.id))

    try:
        pdf_bytes = report_service.render_pdf(html, base_url=request.url_root)
    except Exception as exc:                       # noqa: BLE001
        flash(f"PDF generation failed: {exc}", "error")
        return redirect(url_for("reports.preview", report_id=report.id))

    financial_year = report.financial_year
    if incomplete:
        # A copy to review, stamped on every page. Not the final accounts.
        record("audit_report", report.id, "export_pdf_draft",
               after={"incomplete_notes": len(incomplete)}, commit=True)
    else:
        report.status = "final"
        report.generated_at = datetime.utcnow()
        report.generated_by = current_user.id
        # Only move forward. A closed engagement stays closed, and an
        # engagement still working through customer review is not dragged
        # past that by an export - but one that has simply skipped the
        # statements-version chain should not be stuck at "In Progress"
        # forever either.
        if financial_year.status in ("in_progress", "statements_shared", "approved"):
            financial_year.status = "report_generated"
        record("audit_report", report.id, "export_pdf")
        db.session.commit()

    filename = (f"{financial_year.customer.name}_{financial_year.year_label}"
                f"_Audit_Report{'_DRAFT_INCOMPLETE' if incomplete else ''}.pdf"
                ).replace(" ", "_")

    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf",
                     as_attachment=True, download_name=filename)
