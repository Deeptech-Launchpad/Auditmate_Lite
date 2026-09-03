"""Turning a company profile into the fields of the customer form.

The reading is done by `extraction.ai.extract_company_profile`; this module
translates what it read into the shapes the Customer model stores, and does
nothing else. In particular it never saves: everything here ends up in a form
for a person to look at, because a profile can be out of date, can be the
wrong company, and can be misread, and none of those are discoverable once
the values are already in the database.

Anything that cannot be translated confidently is left out rather than
guessed. A blank field is visibly missing and gets filled in; a wrong one
that looks plausible does not.
"""
import logging
import re
from datetime import date

log = logging.getLogger(__name__)

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# ACRA's wording for what a company is, mapped onto our own vocabulary.
# Ordered: the first phrase found wins, so the more specific entries have to
# come before the phrases they contain.
ENTITY_PHRASES = [
    ("exempt private company", ("private_limited", True)),
    ("private company limited by shares", ("private_limited", False)),
    ("public company limited by guarantee", ("public_limited", False)),
    ("public company", ("public_limited", False)),
    ("limited liability partnership", ("llp", False)),
    ("limited partnership", ("partnership", False)),
    ("sole proprietor", ("sole_proprietorship", False)),
    ("partnership", ("partnership", False)),
    ("branch", ("branch", False)),
    ("private limited", ("private_limited", False)),
]


def _entity(raw):
    """(entity_type, is_exempt_private) from ACRA's own wording.

    Exempt private status is read here rather than asked separately because
    the profile states it in the same phrase as the company type, and it is
    not cosmetic - it decides audit exemption.
    """
    text = (raw or "").strip().lower()
    if not text:
        return None, None
    for phrase, result in ENTITY_PHRASES:
        if phrase in text:
            return result
    return None, None


def _fy_end(raw):
    """(month, day) from '31/12', '31 December', '31-12-2024', 'December'."""
    text = (raw or "").strip().lower()
    if not text:
        return None, None

    for name, number in MONTHS.items():
        if re.search(rf"\b{name}\b", text):
            day = re.search(r"\b(\d{1,2})\b", text)
            return number, int(day.group(1)) if day else None

    # Numeric forms. A year, if one is present, is ignored - a year end is a
    # day and a month, and carrying the year would make it look like a date.
    numbers = [int(n) for n in re.findall(r"\d{1,4}", text)]
    numbers = [n for n in numbers if n <= 31 or n <= 12]
    if len(numbers) >= 2 and 1 <= numbers[1] <= 12 and 1 <= numbers[0] <= 31:
        return numbers[1], numbers[0]
    return None, None


def _iso_date(raw):
    """A date from YYYY-MM-DD, DD/MM/YYYY or DD-MM-YYYY.

    Day-first for the slashed forms, because these are Singapore documents
    and ACRA prints 15/03/2018. An ambiguous pair is left unparsed rather
        than resolved by guessing which way round it goes.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    match = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$", text)
    if match:
        day, month, year = (int(g) for g in match.groups())
        if month > 12:
            return None
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def to_form(profile: dict) -> dict:
    """Profile fields -> customer form values. Absent stays absent."""
    if not profile:
        return {}

    entity_type, exempt = _entity(profile.get("entity_type"))
    fy_month, fy_day = _fy_end(profile.get("financial_year_end"))
    incorporated = _iso_date(profile.get("incorporation_date"))

    directors = profile.get("directors") or []
    if isinstance(directors, str):
        directors = [directors]

    values = {
        "name": (profile.get("name") or "").strip() or None,
        "legal_name": (profile.get("name") or "").strip() or None,
        "uen": (profile.get("uen") or "").strip() or None,
        "entity_type": entity_type,
        "is_exempt_private": exempt,
        "incorporation_date": incorporated,
        "financial_year_end_month": fy_month,
        "financial_year_end_day": fy_day,
        "principal_activities": (profile.get("principal_activities") or "").strip() or None,
        "ssic_code": (profile.get("ssic_code") or "").strip() or None,
        "ssic_description": (profile.get("ssic_description") or "").strip() or None,
        "address_line1": (profile.get("address_line1") or "").strip() or None,
        "address_line2": (profile.get("address_line2") or "").strip() or None,
        "postal_code": (profile.get("postal_code") or "").strip() or None,
        "directors": "\n".join(d.strip() for d in directors if d and d.strip()) or None,
        "company_secretary": (profile.get("company_secretary") or "").strip() or None,
    }
    return {k: v for k, v in values.items() if v is not None}


# What the profile never carries, so the preparer still supplies it. Named
# here so the upload screen can say so plainly rather than leaving someone to
# discover the gaps by scrolling.
NOT_IN_PROFILE = ["Contact person", "Email", "Phone", "GST registration number"]
