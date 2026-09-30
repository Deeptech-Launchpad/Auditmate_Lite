"""Where an answered question's figures land in a note.

One declaration, read by both note-building paths - see
config/answer_bindings.yaml for the reasoning and the shape. This module is
only the reader: it resolves nothing about the accounts, it says which slot
a figure belongs in and what a client might have called it.

Before this, the answer-to-note wiring existed for exactly one question
(KMP) and was spread over two modules as a hardcoded table id, a module
level set of row labels and a bespoke branch in the row filler. Adding a
second note meant adding a second branch. Now it is a config entry.
"""
import functools
import re

import yaml
from flask import current_app

ZERO_FIELDS = ()


@functools.lru_cache(maxsize=1)
def _load():
    path = current_app.config["CONFIG_DIR"] / "answer_bindings.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def items():
    """{item: spec} for every question with a declared landing place."""
    return _load()


def spec(item):
    return _load().get(item)


def note_headings(item):
    """The note's heading and its aliases, for finding it in the library."""
    conf = spec(item) or {}
    headings = [conf.get("note")] + list(conf.get("note_aliases") or [])
    return [h for h in headings if h]


def table_id(item):
    conf = spec(item) or {}
    return conf.get("table_id")


def rows(item):
    """The fixed slots, in the order the Questions page presents them."""
    return list((spec(item) or {}).get("rows") or [])


def row_fields(item):
    return [row["field"] for row in rows(item)]


def row_labels(item):
    return [row.get("label") or row["field"] for row in rows(item)]


def total_spec(item):
    return (spec(item) or {}).get("total")


def matches(patterns, caption):
    """Whether any of `patterns` matches this caption."""
    return _best_match(patterns, caption) is not None


def _best_match(patterns, caption):
    """The length of the longest match any of `patterns` makes, or None.

    Length, not first-match, because a declaration is written by whoever
    adds a note and pattern ORDER must not silently decide correctness.
    "Non-audit fees" matches a slot declared as "audit fee" as well as one
    declared as "non.?audit fee"; the longer match is the more specific
    reading and is the one meant. Getting this wrong is silent - the figure
    lands in the wrong row and the note still prints.
    """
    text = " ".join((caption or "").split())
    best = None
    for pattern in patterns or []:
        found = re.search(pattern, text, re.I)
        if found is None:
            continue
        span = found.end() - found.start()
        if best is None or span > best:
            best = span
    return best


def field_for_caption(item, caption):
    """Which stored field a client's caption is asking for.

    The field name, or the total spec's field when the caption is the whole
    disclosure on one line, or None when it is neither. Slots are preferred
    over the total whatever the match lengths: "short-term employee
    benefits" also matches a loose total pattern, and a slot is the more
    specific reading by its nature.
    """
    best_field, best_span = None, -1
    for row in rows(item):
        span = _best_match(row.get("patterns"), caption)
        if span is not None and span > best_span:
            best_field, best_span = row["field"], span
    if best_field:
        return best_field

    total = total_spec(item)
    if total and _best_match(total.get("patterns"), caption) is not None:
        return total["field"]
    return None


def resolve_caption(caption):
    """(item, field) for a caption across every declared note, or None.

    One entry point for the template path, so it resolves a caption the same
    way and by the same best-match rule as everything else rather than
    walking a flattened pattern list and taking whatever matched first.
    """
    best = None
    for item in _load():
        field = field_for_caption(item, caption)
        if not field:
            continue
        if is_total_field(item, field):
            span = _best_match((total_spec(item) or {}).get("patterns"), caption)
            is_slot = False
        else:
            row = next(r for r in rows(item) if r["field"] == field)
            span = _best_match(row.get("patterns"), caption)
            is_slot = True
        # A slot anywhere beats a total anywhere; then longest match wins.
        rank = (1 if is_slot else 0, span or 0)
        if best is None or rank > best[0]:
            best = (rank, (item, field))
    return best[1] if best else None


def is_total_field(item, field):
    total = total_spec(item)
    return bool(total and field == total.get("field"))


def stored_total(financial_year, item):
    """The sum of this item's answered slots, or None if none are answered.

    What a client who prints the disclosure as one line should show. None
    rather than zero when nothing is answered, because nil and unanswered
    are different statements about the company.
    """
    from decimal import Decimal

    from . import document_fields

    scope = table_id(item)
    total, answered = Decimal("0"), False
    for field in row_fields(item):
        row = document_fields.value(financial_year, "ENTERED", field, scope)
        if row is None or row.amount is None:
            continue
        total += Decimal(str(row.amount))
        answered = True
    return total if answered else None


def stored_field(financial_year, item, field):
    """One slot's answered figure, or None. Handles the total field too."""
    from decimal import Decimal

    from . import document_fields

    if is_total_field(item, field):
        return stored_total(financial_year, item)
    row = document_fields.value(financial_year, "ENTERED", field,
                                table_id(item))
    if row is None or row.amount is None:
        return None
    return Decimal(str(row.amount))
