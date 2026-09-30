"""Where an answered question's figures land in a note.

Covers acceptance criteria C1, C2 and C3.

The point of C2 is that this is no longer a hardcode: a second note is a
config entry, not a branch. That is asserted by adding one in the test and
exercising the whole path with no production code changed.
"""
from decimal import Decimal

import pytest

from app.services import answer_bindings as ab
from app.services import document_fields
from app.services import template_note_tables as tnt

from . import factories as f


@pytest.fixture(autouse=True)
def _clear_config_cache():
    """The declaration is lru_cached per process; a test that swaps it must
    not leak into the next one."""
    ab._load.cache_clear()
    yield
    ab._load.cache_clear()


def _answer(fy, field, amount, item="KMP_SPLIT"):
    document_fields.save(fy, "ENTERED", field, scope=ab.table_id(item),
                         amount=Decimal(str(amount)))


# --- the declaration itself ----------------------------------------------

def test_kmp_is_declared_in_config(app):
    assert "KMP_SPLIT" in ab.items()
    assert ab.table_id("KMP_SPLIT") == "N59_KEY_MANAGEMENT_PERSONNEL_T2"


def test_stored_field_names_are_the_ones_already_in_use(app):
    """These are what figures already answered are keyed by. Renaming one
    orphans a preparer's answer, so it is pinned."""
    assert ab.row_fields("KMP_SPLIT") == [
        "short_term_employee_benefits",
        "post_employment_benefits",
        "other_long_term_benefits",
        "termination_benefits",
        "share_based_payment",
    ]


# --- caption matching ----------------------------------------------------

def test_slot_captions_resolve_to_their_slot(app):
    assert ab.field_for_caption("KMP_SPLIT", "Short-term employee benefits") \
        == "short_term_employee_benefits"
    assert ab.field_for_caption("KMP_SPLIT", "Post-employment benefits") \
        == "post_employment_benefits"


def test_a_combined_single_line_resolves_to_the_total(app):
    """C3. The client's own template presents the whole disclosure on one
    line instead of the five slots."""
    caption = "Key management personnel services provided by a management entity"
    assert ab.field_for_caption("KMP_SPLIT", caption) == "kmp_total"
    assert ab.is_total_field("KMP_SPLIT", "kmp_total")


def test_the_total_is_matched_by_concept_not_one_clients_sentence(app):
    """The fix must not be that one customer's sentence is hardcoded. Other
    wordings of the same disclosure resolve to the same slot."""
    for caption in ["Key management personnel compensation",
                    "Key management personnel remuneration",
                    "Compensation of key management personnel",
                    "Directors' and key management personnel benefits"]:
        assert ab.field_for_caption("KMP_SPLIT", caption) == "kmp_total", caption


def test_slots_win_over_the_total(app):
    """"Short-term employee benefits" also contains "benefit" and would
    match a loose total pattern; the more specific reading is right."""
    assert ab.field_for_caption("KMP_SPLIT", "Short-term employee benefits") \
        != "kmp_total"


def test_ambiguous_prefix_does_not_win_on_pattern_order(app, monkeypatch):
    """Found while writing the second-note test: "Non-audit fees" matched a
    slot declared "audit fee" as well as one declared "non.?audit fee",
    and whichever pattern happened to be declared first won regardless of
    which slot actually fit. Guard the rule directly: the longer, more
    specific match must win, not declaration order."""
    config = {"FEES": {
        "note": "Auditor's remuneration", "table_id": "T1",
        "rows": [
            {"field": "audit", "label": "Audit fees", "patterns": ["audit fee"]},
            {"field": "non_audit", "label": "Non-audit fees",
             "patterns": ["non.?audit fee"]},
        ],
    }}
    monkeypatch.setattr(ab, "_load", lambda: config)
    assert ab.field_for_caption("FEES", "Non-audit fees") == "non_audit"
    assert ab.field_for_caption("FEES", "Audit fees") == "audit"


def test_unrelated_captions_do_not_match(app):
    for caption in ["Staff costs (including director's fee)",
                    "Legal and professional fee",
                    "Trade receivables"]:
        assert ab.field_for_caption("KMP_SPLIT", caption) is None, caption


# --- reading the answers back --------------------------------------------

def test_total_is_the_sum_of_answered_slots(db):
    fy = f.financial_year()
    _answer(fy, "short_term_employee_benefits", 10000)
    _answer(fy, "post_employment_benefits", 22436)
    assert ab.stored_total(fy, "KMP_SPLIT") == Decimal("32436")


def test_total_is_none_when_nothing_is_answered(db):
    """Unanswered and nil are different statements about the company, so an
    unanswered total must not read as zero."""
    fy = f.financial_year()
    assert ab.stored_total(fy, "KMP_SPLIT") is None


def test_stored_field_reads_one_slot(db):
    fy = f.financial_year()
    _answer(fy, "short_term_employee_benefits", 10000)
    assert ab.stored_field(fy, "KMP_SPLIT", "short_term_employee_benefits") \
        == Decimal("10000")
    assert ab.stored_field(fy, "KMP_SPLIT", "termination_benefits") is None


# --- the template-following path ------------------------------------------

def test_combined_line_prints_the_total(db):
    """C1. The client's own single combined caption gets the answered
    total, which is what was missing: the figure was recorded and the note
    printed nothing."""
    fy = f.financial_year()
    _answer(fy, "short_term_employee_benefits", 10000)
    _answer(fy, "post_employment_benefits", 22436)

    value, _codes = tnt._fill_row(
        "Key management personnel services provided by a management entity",
        None, fy)
    assert value == Decimal("32436")


def test_each_slot_prints_its_own_answer(db):
    fy = f.financial_year()
    _answer(fy, "short_term_employee_benefits", 10000)
    value, _ = tnt._fill_row("Short-term employee benefits", None, fy)
    assert value == Decimal("10000")


def test_an_unanswered_slot_is_offered_for_answering(db):
    """Held, and typeable: the cell carries the token/field/scope that
    document_fields.save() needs, so the preparer can answer it in place."""
    fy = f.financial_year()
    value, _ = tnt._fill_row("Termination benefits", None, fy)
    assert hasattr(value, "reason")
    assert value.token == "ENTERED"
    assert value.field == "termination_benefits"
    assert value.scope == ab.table_id("KMP_SPLIT")


def test_an_unanswered_total_is_not_typeable(db):
    """A total row is the sum of the slots. If it could be typed in its own
    right the note would print two versions of one fact."""
    fy = f.financial_year()
    value, _ = tnt._fill_row("Key management personnel compensation", None, fy)
    assert hasattr(value, "reason")
    assert not value.editable


# --- C2: a second note, by config alone -----------------------------------

SECOND_NOTE = {
    "note": "Auditor's remuneration",
    "table_id": "N99_AUDITOR_REMUNERATION_T1",
    "rows": [
        {"field": "audit_fees", "label": "Audit fees",
         "patterns": ["audit fee", "fees for the audit"]},
        {"field": "non_audit_fees", "label": "Non-audit fees",
         "patterns": ["non.?audit fee", "other services"]},
    ],
    "total": {"field": "auditor_total", "label": "Auditor's remuneration",
              "patterns": ["auditor.{0,3} remuneration", "total auditor"]},
}


def test_a_second_note_works_with_no_code_change(db, monkeypatch):
    """C2. The whole point. Nothing in app/ is touched - only the
    declaration - and the new note resolves, stores and prints."""
    config = dict(ab._load())
    config["AUDITOR_REMUNERATION"] = SECOND_NOTE
    monkeypatch.setattr(ab, "_load", lambda: config)

    fy = f.financial_year()

    # captions resolve
    assert ab.field_for_caption("AUDITOR_REMUNERATION", "Audit fees") \
        == "audit_fees"
    assert ab.field_for_caption("AUDITOR_REMUNERATION", "Non-audit fees") \
        == "non_audit_fees"
    assert ab.field_for_caption("AUDITOR_REMUNERATION", "Auditor's remuneration") \
        == "auditor_total"

    # answers store and read back
    document_fields.save(fy, "ENTERED", "audit_fees",
                         scope="N99_AUDITOR_REMUNERATION_T1",
                         amount=Decimal("4500"))
    document_fields.save(fy, "ENTERED", "non_audit_fees",
                         scope="N99_AUDITOR_REMUNERATION_T1",
                         amount=Decimal("1200"))
    assert ab.stored_total(fy, "AUDITOR_REMUNERATION") == Decimal("5700")

    # and the template path prints them
    value, _ = tnt._fill_row("Audit fees", None, fy)
    assert value == Decimal("4500")
    value, _ = tnt._fill_row("Total auditor remuneration", None, fy)
    assert value == Decimal("5700")


def test_the_two_notes_do_not_bleed_into_each_other(db, monkeypatch):
    config = dict(ab._load())
    config["AUDITOR_REMUNERATION"] = SECOND_NOTE
    monkeypatch.setattr(ab, "_load", lambda: config)

    fy = f.financial_year()
    _answer(fy, "short_term_employee_benefits", 10000)

    assert ab.stored_total(fy, "KMP_SPLIT") == Decimal("10000")
    assert ab.stored_total(fy, "AUDITOR_REMUNERATION") is None
    assert ab.field_for_caption("KMP_SPLIT", "Audit fees") is None
    assert ab.field_for_caption("AUDITOR_REMUNERATION",
                                "Short-term employee benefits") is None
