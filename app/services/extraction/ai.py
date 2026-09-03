"""AI extraction - provider-neutral.

Why this exists: rule-based parsers are exact on clean spreadsheets, but real
audit files are frequently scanned PDFs, photographed documents, or layouts
whose columns don't line up. Those are exactly the cases where a misread
number would flow silently into a financial statement - so this is where AI
earns its place.

Two jobs:
  1. `extract_with_ai`   - read a document and return structured line items.
  2. `classify_accounts` - map unfamiliar account labels onto statement lines.

The actual model call lives in `providers/` (Claude or Gemini, chosen by
AI_PROVIDER in .env). Everything here - the schemas, the prompts, the
confidence handling - is shared, so switching provider changes no behaviour
downstream.

Design notes:
  * PDFs and images are sent to the model directly; both providers read
    scanned pages, so there is no local OCR step and no Tesseract dependency.
  * Output is constrained by a Pydantic schema, so the response is validated
    structured data rather than text we'd have to guess our way through.
  * The model reports its own per-row confidence. Anything it is unsure about
    is surfaced to the auditor rather than silently accepted.
  * Nothing here is trusted blindly - every AI-produced row still lands in the
    Review & Correct screen for a human to confirm.
"""
import logging
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

from flask import current_app
from pydantic import BaseModel, Field

from .base import ExtractedRow, ExtractionResult
from .providers import get_provider, provider_name

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Response schemas - these constrain what the model is allowed to return.
# --------------------------------------------------------------------------

class AILineItem(BaseModel):
    label: str = Field(description="The account name or line description, cleaned up")
    account_code: Optional[str] = Field(
        default=None, description="Account/GL code if the document shows one")
    amount: Optional[float] = Field(
        default=None, description="Single amount, if the row has one value column")
    debit: Optional[float] = Field(
        default=None, description="Debit amount if the document has debit/credit columns")
    credit: Optional[float] = Field(
        default=None, description="Credit amount if the document has debit/credit columns")
    period: str = Field(
        default="current",
        description="'current' or 'previous' - 'previous' for comparative-year columns")
    page: Optional[int] = Field(
        default=None, description="Page number this row was read from")
    confidence: float = Field(
        default=0.9,
        description="Your confidence 0.0-1.0 that this row was read correctly. "
                    "Be honest - low values get flagged for human review.")


class AIExtraction(BaseModel):
    document_kind: str = Field(
        description="What this document appears to be, e.g. 'trial balance', "
                    "'bank statement', 'invoice'")
    currency: Optional[str] = Field(default=None, description="Currency code if visible")
    line_items: List[AILineItem] = Field(description="Every financial line found")
    notes: Optional[str] = Field(
        default=None,
        description="Anything the auditor should know: unreadable sections, "
                    "ambiguity, totals that don't add up")


class AIPriorNote(BaseModel):
    note_number: Optional[str] = Field(
        default=None,
        description="The note number as printed, e.g. '3' or '3(a)'. Null for "
                    "an unnumbered section such as corporate information.")
    title: str = Field(description="The note heading, as printed")
    body_text: str = Field(
        description="The note's full narrative text, verbatim. Keep the "
                    "company's own wording exactly - do not summarise, "
                    "paraphrase, tidy or shorten it.")
    confidence: float = Field(
        default=0.9,
        description="0.0-1.0 that this note was read correctly. Score below "
                    "0.8 if the page was blurred or the text ran across a "
                    "column or page break you had to infer.")


class AIPriorNotes(BaseModel):
    notes: List[AIPriorNote] = Field(
        description="Every note found, in the order printed")
    unreadable: Optional[str] = Field(
        default=None,
        description="Say so plainly if the notes could not be read at all, "
                    "or only partly - e.g. 'pages 6-8 are a scan too faint "
                    "to read'. Null when everything was readable.")


class AICompanyProfile(BaseModel):
    """Company particulars as an ACRA Business Profile states them."""

    name: Optional[str] = Field(default=None, description="Registered company name")
    uen: Optional[str] = Field(
        default=None, description="Unique Entity Number, e.g. 201812345K")
    entity_type: Optional[str] = Field(
        default=None,
        description="Company type exactly as printed, e.g. 'EXEMPT PRIVATE "
                    "COMPANY LIMITED BY SHARES' or 'PRIVATE COMPANY LIMITED "
                    "BY SHARES'")
    incorporation_date: Optional[str] = Field(
        default=None, description="Date of incorporation as YYYY-MM-DD")
    financial_year_end: Optional[str] = Field(
        default=None,
        description="The financial year end as printed, e.g. '31 December' "
                    "or '30 June'. Day and month only - not a year.")
    principal_activities: Optional[str] = Field(
        default=None,
        description="The principal activity description, in the profile's "
                    "own words. If a primary and a secondary activity are "
                    "both given, return the primary one.")
    ssic_code: Optional[str] = Field(
        default=None, description="The primary SSIC code, digits only")
    ssic_description: Optional[str] = Field(
        default=None, description="The primary SSIC activity description")
    address_line1: Optional[str] = Field(
        default=None, description="Registered office address, first line")
    address_line2: Optional[str] = Field(
        default=None, description="Registered office address, second line")
    postal_code: Optional[str] = Field(default=None, description="Postal code")
    directors: Optional[List[str]] = Field(
        default=None,
        description="Names of people holding the position of DIRECTOR. Names "
                    "only. Exclude shareholders, the secretary, and anyone "
                    "whose appointment has ceased.")
    company_secretary: Optional[str] = Field(
        default=None, description="Name of the company secretary")
    confidence: float = Field(
        default=0.9,
        description="0.0-1.0 that this document really is a company profile "
                    "and was read correctly. Score low if it is some other "
                    "kind of document.")
    notes: Optional[str] = Field(
        default=None,
        description="Anything unreadable or absent that a preparer should "
                    "check by hand. Null when the profile read cleanly.")


class AIAccountMapping(BaseModel):
    label: str = Field(description="The original account label given to you")
    line_key: str = Field(description="The statement line key you are mapping it to")
    confidence: float = Field(description="0.0-1.0 confidence in this mapping")
    reasoning: str = Field(description="One short sentence explaining the choice")


class AIAccountMappings(BaseModel):
    mappings: List[AIAccountMapping]


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------

def ai_available() -> bool:
    """True when the configured provider has a key."""
    try:
        return get_provider().available()
    except Exception:                              # noqa: BLE001
        return False


def active_provider_label() -> str:
    """Human-readable name of the engine in use, for the UI."""
    try:
        return get_provider().LABEL
    except Exception:                              # noqa: BLE001
        return provider_name().title()


def test_connection() -> dict:
    """Check the configured provider's key with a tiny real request."""
    try:
        provider = get_provider()
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not provider.available():
        return {"ok": False,
                "error": f"No API key set for provider {provider_name()!r}"}
    result = provider.test_connection()
    result["provider"] = provider.LABEL
    return result


EXTRACTION_SYSTEM_PROMPT = """You are a data extraction assistant for a \
Singapore audit firm. You read accounting documents and return the financial \
line items they contain.

Rules:
- Extract every account line with a monetary value. Do not summarise, \
aggregate, or skip rows.
- Preserve the document's own wording for account labels. Do not rename \
"Sundry Debtors" to "Trade Receivables" - mapping happens later.
- If the document has separate Debit and Credit columns, populate `debit` and \
`credit`. If it has a single amount column, populate `amount` only.
- Negative numbers may appear in parentheses, e.g. (1,234.00) means -1234.00.
- Amounts may carry Cr/Dr suffixes or S$ prefixes - return clean numbers.
- PERIODS. Read the column headings before reading any figure. Where a \
document prints more than one dated column - "31 Dec 2024" beside "31 Dec \
2023", or "Current Year" beside "Prior Year" - every figure belongs to \
exactly one of them, and you must say which. Emit ONE ROW PER ACCOUNT PER \
PERIOD: the account's current-year figure with period="current", and a \
second row, same label, carrying its prior-year figure with \
period="previous". The latest date is the current year; every earlier date \
is a comparative. Never emit two rows for the same account without \
distinguishing them, and never merge two years' figures into one row. A \
prior-year balance recorded as this year's goes straight onto the face of \
the financial statements.
- Skip page headers, footers and page numbers. DO keep genuine total lines, \
labelled exactly as the document labels them.
- Set `confidence` honestly per row. If a figure is blurred, ambiguous, or you \
inferred a column alignment, score it below 0.8 so a human checks it. \
Accuracy matters more than completeness here - a flagged row is cheap, a \
wrong number in a financial statement is not."""


CLASSIFY_SYSTEM_PROMPT = """You map accounting labels onto financial statement \
lines for Singapore FRS financial statements.

You will be given a list of account labels from a client's books, and the set \
of valid statement line keys. For each label, choose the single best line key.

Guidance:
- "Sundry Debtors", "Trade Debtors", "Accounts Receivable" -> trade_receivables
- "Sundry Creditors", "Trade Creditors" -> trade_payables
- "Cash at Bank", "Bank OCBC", "Petty Cash" -> cash_and_equivalents
- "Turnover", "Sales", "Fee Income" -> revenue
- Directors' remuneration is a separate line from general staff costs.
- If a label genuinely doesn't fit any available key, use "unmapped" and give \
it a low confidence. Do not force a bad match - an unmapped item gets a human \
decision, a wrong match silently corrupts the accounts."""


# --------------------------------------------------------------------------
# Document extraction
# --------------------------------------------------------------------------

def extract_with_ai(path: Path, file_type: str, category: str = "other",
                    raw_text: str = "") -> ExtractionResult:
    """Send a document to the configured model and return structured rows."""
    result = ExtractionResult(engine="ai", ai_used=True)

    try:
        provider = get_provider()
    except ValueError as exc:
        result.error = str(exc)
        return result

    if not provider.available():
        result.error = (f"No API key configured for {provider.LABEL}. "
                        f"Set it in .env and restart.")
        return result

    result.engine = provider_name()
    category_hint = (f"The auditor filed this document under the category "
                     f"'{category}'. ") if category != "other" else ""

    try:
        parts = []

        if file_type == "pdf":
            parts.append({"type": "pdf", "data": path.read_bytes()})
        elif file_type == "image":
            suffix = path.suffix.lower()
            mime = "image/png" if suffix == ".png" else "image/jpeg"
            parts.append({"type": "image", "data": path.read_bytes(),
                          "mime": mime})
        elif raw_text:
            # Word docs / CSVs the rule parser struggled with: send the text.
            parts.append({"type": "text",
                          "text": f"Document contents:\n\n{raw_text[:100000]}"})
        else:
            result.error = "Nothing to send to the AI provider"
            return result

        parts.append({
            "type": "text",
            "text": (f"{category_hint}Extract all financial line items from "
                     f"this document. Return every account line with its "
                     f"amounts."),
        })

        parsed = provider.structured_call(
            EXTRACTION_SYSTEM_PROMPT, parts, AIExtraction, max_tokens=16000)

        for item in parsed.line_items:
            result.rows.append(ExtractedRow(
                label=item.label,
                raw_label=item.label,
                amount=Decimal(str(item.amount)) if item.amount is not None else None,
                debit=Decimal(str(item.debit)) if item.debit is not None else None,
                credit=Decimal(str(item.credit)) if item.credit is not None else None,
                account_code=item.account_code,
                account_type=getattr(item, 'account_type', None),
                period=item.period if item.period in ("current", "previous") else "current",
                source_ref={"page": item.page} if item.page else {"source": "ai"},
                confidence=max(0.0, min(1.0, item.confidence)),
            ))

        if parsed.notes:
            result.raw_text = parsed.notes

        log.info("%s extracted %d rows from %s",
                 provider.LABEL, len(result.rows), path.name)

    except Exception as exc:                       # noqa: BLE001
        log.exception("AI extraction failed")
        result.error = f"AI extraction failed ({provider.LABEL}): {exc}"

    return result


COMPANY_PROFILE_SYSTEM_PROMPT = """You read Singapore company particulars out \
of an ACRA BizFile Business Profile, or out of a set of signed financial \
statements, so a preparer does not have to type them again.

Rules:
- Copy what the document says. Do not tidy, expand or normalise a company \
name, an address or an activity description.
- Return the REGISTERED office address, not a correspondence or business \
address, when the document distinguishes them.
- Directors are people listed with the position of DIRECTOR. Do not include \
shareholders, the company secretary, or officers whose appointment has \
ceased - a ceased director named as current would go onto the cover of the \
financial statements.
- The company type matters and must be copied exactly as printed. An "EXEMPT \
PRIVATE COMPANY LIMITED BY SHARES" is not the same as a "PRIVATE COMPANY \
LIMITED BY SHARES", and the difference decides audit exemption.
- A field the document does not state is null. Never infer, never guess a \
plausible value: a wrong UEN or incorporation date is worse than a blank one \
the preparer fills in, because a blank is visibly missing and a wrong value \
is not.
- If this document is not a company profile or a set of financial statements \
at all, score confidence below 0.4 and say so in notes."""


def extract_company_profile(path: Path, file_type: str,
                            raw_text: str = "") -> dict:
    """Read company particulars out of an ACRA profile or signed accounts.

    Returns {"ok", "profile": {...}, "error"}. Nothing is saved here and
    nothing is decided here - the caller puts these into a form for a person
    to check before any of it becomes a customer record.
    """
    try:
        provider = get_provider()
    except ValueError as exc:
        return {"ok": False, "profile": {}, "error": str(exc)}

    if not provider.available():
        return {"ok": False, "profile": {},
                "error": f"No API key configured for {provider.LABEL}."}

    parts = []
    if file_type == "pdf":
        parts.append({"type": "pdf", "data": path.read_bytes()})
    elif file_type == "image":
        suffix = path.suffix.lower()
        mime = "image/png" if suffix == ".png" else "image/jpeg"
        parts.append({"type": "image", "data": path.read_bytes(), "mime": mime})
    elif raw_text:
        parts.append({"type": "text",
                      "text": f"Document contents:\n\n{raw_text[:100000]}"})
    else:
        return {"ok": False, "profile": {},
                "error": "Nothing to send to the AI provider"}

    parts.append({
        "type": "text",
        "text": ("Read this company's particulars. Return only what the "
                 "document actually states; leave anything absent null."),
    })

    try:
        parsed = provider.structured_call(
            COMPANY_PROFILE_SYSTEM_PROMPT, parts, AICompanyProfile,
            max_tokens=8000)
    except Exception as exc:                       # noqa: BLE001
        log.exception("Company profile extraction failed")
        return {"ok": False, "profile": {},
                "error": f"Could not read this document ({provider.LABEL}): {exc}"}

    profile = parsed.model_dump()
    log.info("%s read a company profile for %r (confidence %.2f)",
             provider.LABEL, profile.get("name"), parsed.confidence)

    if parsed.confidence < 0.4:
        return {"ok": False, "profile": profile,
                "error": (parsed.notes or "This does not look like a company "
                          "profile or a set of financial statements.")}

    return {"ok": True, "profile": profile, "error": None}


PRIOR_NOTES_SYSTEM_PROMPT = """You read the notes to the financial statements \
out of a set of signed Singapore company accounts, so that next year's \
accounts can show what was disclosed last year.

You are reading NARRATIVE, not figures. Another process already reads the \
numbers; your job is the words.

Rules:
- Return every note, including unnumbered front sections such as corporate \
information / general information, and the accounting policy notes.
- Reproduce each note's wording VERBATIM. Do not summarise, paraphrase, \
modernise, correct grammar, or shorten. The point of reading these is to \
recover the company's own sentences - a tidied version is worthless.
- Where a note contains a table of figures, keep the surrounding narrative \
and leave the table out. The figures come from elsewhere.
- Keep the note number exactly as printed, including any sub-letter: "3", \
"3(a)", "12.1". Leave it null for a section printed without a number.
- Company-specific sentences matter most: principal activities and the SSIC \
description, credit terms granted to customers, useful lives and \
depreciation rates, the basis of any estimate. Never drop these.
- If you cannot read part of the document - a faint scan, a missing page - \
say which part in `unreadable` rather than inventing what it probably said. \
An honest gap is useful; a plausible fabrication is dangerous."""


def extract_prior_year_notes(path: Path, file_type: str,
                             raw_text: str = "") -> dict:
    """Read the note wording out of last year's signed accounts.

    Returns {"ok", "notes": [...], "unreadable": str|None, "error": str|None}.
    Figures are not touched here - `extract_with_ai` already reads those, and
    the comparative column is built from them.
    """
    try:
        provider = get_provider()
    except ValueError as exc:
        return {"ok": False, "notes": [], "unreadable": None,
                "error": str(exc)}

    if not provider.available():
        return {"ok": False, "notes": [], "unreadable": None,
                "error": f"No API key configured for {provider.LABEL}."}

    parts = []
    if file_type == "pdf":
        parts.append({"type": "pdf", "data": path.read_bytes()})
    elif file_type == "image":
        suffix = path.suffix.lower()
        mime = "image/png" if suffix == ".png" else "image/jpeg"
        parts.append({"type": "image", "data": path.read_bytes(), "mime": mime})
    elif raw_text:
        parts.append({"type": "text",
                      "text": f"Document contents:\n\n{raw_text[:100000]}"})
    else:
        return {"ok": False, "notes": [], "unreadable": None,
                "error": "Nothing to send to the AI provider"}

    parts.append({
        "type": "text",
        "text": ("Read the notes to the financial statements out of these "
                 "signed accounts. Return each note's number, heading and "
                 "full narrative text, word for word."),
    })

    try:
        parsed = provider.structured_call(
            PRIOR_NOTES_SYSTEM_PROMPT, parts, AIPriorNotes, max_tokens=32000)
    except Exception as exc:                       # noqa: BLE001
        log.exception("Prior-year note extraction failed")
        return {"ok": False, "notes": [], "unreadable": None,
                "error": f"Could not read the notes ({provider.LABEL}): {exc}"}

    notes = [{"note_number": (n.note_number or "").strip() or None,
              "title": n.title.strip(),
              "body_text": n.body_text,
              "confidence": max(0.0, min(1.0, n.confidence))}
             for n in parsed.notes if n.title and n.title.strip()]

    log.info("%s read %d prior-year note(s) from %s",
             provider.LABEL, len(notes), path.name)

    return {"ok": bool(notes), "notes": notes,
            "unreadable": parsed.unreadable, "error": None}


# --------------------------------------------------------------------------
# Account classification
# --------------------------------------------------------------------------

def classify_accounts(labels: List[str], line_keys: List[str],
                      statement_type: str) -> dict:
    """Ask the model to map unfamiliar account labels onto statement lines.

    Only called for labels the deterministic rules could not match, so the
    cost stays proportional to how unusual the client's chart of accounts is.
    Returns {label: {"line_key", "confidence", "reasoning"}}.
    """
    if not labels:
        return {}

    try:
        provider = get_provider()
    except ValueError:
        return {}
    if not provider.available():
        return {}

    prompt = (
        f"Statement type: {statement_type}\n\n"
        "Valid line keys:\n" + "\n".join(f"- {k}" for k in line_keys) +
        "\n- unmapped\n\n"
        "Account labels to map:\n" + "\n".join(f"- {l}" for l in labels)
    )

    try:
        parsed = provider.structured_call(
            CLASSIFY_SYSTEM_PROMPT, [{"type": "text", "text": prompt}],
            AIAccountMappings, max_tokens=8000)

        return {
            m.label: {"line_key": m.line_key,
                      "confidence": m.confidence,
                      "reasoning": m.reasoning}
            for m in parsed.mappings
        }

    except Exception:                              # noqa: BLE001
        log.exception("AI account classification failed")
        return {}
