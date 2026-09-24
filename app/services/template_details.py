"""The company's details, read from the template it brought - no AI needed.

The template is last year's signed accounts, and the first pages state the
company's name, registration number and director(s), and note 1 states the
registered office and principal activities. Reading those with the AI service
alone meant an outage, or a template uploaded before the feature existed, left
the customer record empty and the report full of "[not provided]".

This reads the same facts from the words the accounts use, and fills only what
the customer record has EMPTY - nothing a person typed is touched. Anything it
cannot find is left for the customer page.
"""
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_UEN = re.compile(r"Registration\s*(?:Number|No\.?)\s*[:.]?\s*([0-9]{8,10}[A-Z])",
                  re.IGNORECASE)
_DIRECTORS = re.compile(
    r"directors?\s+of\s+the\s+company\s+in\s+office[^\n:]*:\s*\n?(?P<names>.+?)"
    r"(?:\n\s*\n|\n\s*\d+\.\s|\Z)", re.IGNORECASE | re.DOTALL)
_OFFICE = re.compile(
    r"registered\s+office\s+at\s+(?P<addr>.+?)\.(?:\s|\Z)", re.IGNORECASE | re.DOTALL)
_ACTIVITIES = re.compile(
    r"principal\s+activit(?:y|ies)\s+of\s+the\s+company\s+(?:is|are)\s+"
    r"(?P<text>.+?)\.(?:\s|\Z)", re.IGNORECASE | re.DOTALL)
_POSTAL = re.compile(r"(?:Singapore\s*)?(\d{6})\s*$", re.IGNORECASE)
_SECRETARY = re.compile(r"company\s+secretary\s*[:\n]+\s*(?P<name>[^\n]+)",
                        re.IGNORECASE)


def _text(path, pages=40):
    path = Path(str(path))
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages[:pages])
    if suffix == ".docx":
        import docx

        document = docx.Document(str(path))
        return "\n".join(p.text for p in document.paragraphs)
    return ""


def _clean(text):
    return " ".join((text or "").split())


def read(path):
    """{"legal_name", "uen", "directors", "address_line1", "address_line2",
    "postal_code", "principal_activities", "company_secretary"} - whichever the
    template states. Empty where it does not."""
    try:
        text = _text(path)
    except Exception:                                        # noqa: BLE001
        log.exception("Could not read the template %s", path)
        return {}
    if not text.strip():
        return {}

    found = {}
    match = _UEN.search(text)
    if match:
        found["uen"] = match.group(1).upper()
        # The company's own name is the line above its registration number.
        before = text[:match.start()].splitlines()
        for line in reversed(before):
            line = line.strip()
            if line and not re.search(r"registration|\(|financial|statement",
                                      line, re.IGNORECASE):
                found["legal_name"] = line.title().replace("Pte.", "Pte.") \
                    if line.isupper() else line
                break

    match = _DIRECTORS.search(text)
    if match:
        names = [ln.strip() for ln in match.group("names").splitlines()
                 if ln.strip() and len(ln.strip()) <= 80
                 and not re.search(r"[.;:]$", ln.strip())]
        if names:
            found["directors"] = "\n".join(names)

    match = _OFFICE.search(text)
    if match:
        address = _clean(match.group("addr"))
        postal = _POSTAL.search(address)
        if postal:
            found["postal_code"] = postal.group(1)
            address = _POSTAL.sub("", address).strip(" ,")
        # "10 Ubi Crescent #06-51 Ubi Techpark" - the unit and building are
        # a second line where the address has one.
        unit = re.search(r"\s(#\d+-\d+.*)$", address)
        if unit:
            found["address_line1"] = address[:unit.start()].strip()
            found["address_line2"] = unit.group(1).strip()
        else:
            found["address_line1"] = address

    match = _ACTIVITIES.search(text)
    if match:
        found["principal_activities"] = _clean(match.group("text"))

    match = _SECRETARY.search(text)
    if match:
        found["company_secretary"] = match.group("name").strip()
    return found


FILLABLE = ("legal_name", "uen", "directors", "address_line1", "address_line2",
            "postal_code", "principal_activities", "company_secretary")


def fill_missing(customer):
    """Fill the customer's EMPTY details from their template. Returns the
    names of the fields filled (none if nothing was empty or nothing found)."""
    path = getattr(customer, "report_template_path", None)
    if not path or not Path(str(path)).exists():
        return []
    empty = [f for f in FILLABLE if not getattr(customer, f, None)]
    if not empty:
        return []
    found = read(path)
    filled = []
    for field in empty:
        value = found.get(field)
        if value:
            setattr(customer, field, value)
            filled.append(field)
    return filled
