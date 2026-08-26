"""Shared types, number normalisation and confidence scoring.

Every extractor — rule-based or AI — returns the same `ExtractionResult`, so
downstream code never needs to know which engine produced a row.
"""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
import re

# Base confidence per engine. Deterministic parsers score high because a
# number read out of a spreadsheet cell is exactly that number; PDF table
# detection is inference, so it starts lower.
AI_ENGINES = {"anthropic", "gemini", "claude"}

ENGINE_CONFIDENCE = {
    "openpyxl": 0.95,
    "csv": 0.95,
    "python-docx": 0.85,
    "pdfplumber": 0.75,
    # AI back-ends. The model reports its own per-row
    # confidence, which overrides this baseline (see score_row).
    "anthropic": 0.88,
    "gemini": 0.88,
    "claude": 0.88,      # legacy value, kept for old rows
}


@dataclass
class ExtractedRow:
    """One candidate line item found in a document."""
    label: str = ""
    amount: Optional[Decimal] = None
    debit: Optional[Decimal] = None
    credit: Optional[Decimal] = None
    account_code: Optional[str] = None
    period: str = "current"
    raw_label: str = ""
    raw_values: list = field(default_factory=list)
    source_ref: dict = field(default_factory=dict)
    confidence: float = 1.0
    needs_review: bool = False


@dataclass
class ExtractionResult:
    engine: str = ""
    rows: list = field(default_factory=list)
    page_count: Optional[int] = None
    ai_used: bool = False
    error: Optional[str] = None
    # Raw text kept so the AI fallback can re-read the document without
    # touching disk again.
    raw_text: str = ""

    @property
    def confidence(self) -> float:
        if not self.rows:
            return 0.0
        return sum(r.confidence for r in self.rows) / len(self.rows)


# --------------------------------------------------------------------------
# Number normalisation
#
# This is where most real-world files break. Accounting exports are full of
# formatting that looks like a number to a human and like a string to Python.
# --------------------------------------------------------------------------

_CURRENCY_CHARS = "$€£¥₹"
# The cleaned cell must be entirely a number for it to count as an amount.
_STRICT_NUM_RE = re.compile(r"^[\d,]*\.?\d+$")


def parse_amount(value: Any) -> Optional[Decimal]:
    """Turn a spreadsheet cell / OCR fragment into a Decimal, or None.

    Handles: parenthesised negatives "(1,234.00)" -> -1234.00,
             trailing Cr/Dr markers, currency symbols and codes,
             unicode minus and en-dash, thousands separators,
             blank / "-" / "NIL" placeholders.
    """
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    # Placeholders that mean "nothing here", not zero.
    if text.upper() in {"-", "--", "—", "NIL", "N/A", "NA", "", "."}:
        return None

    negative = False

    # Accounting negatives: (1,234.00)
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]

    # Trailing credit/debit markers, common in ledger exports.
    upper = text.upper()
    if upper.endswith("CR"):
        negative = True
        text = text[:-2]
    elif upper.endswith("DR"):
        text = text[:-2]

    # Strip currency markers. Prefixed forms like "S$" must go first:
    # removing the bare "$" up front would strand the "S" and leave a cell
    # that no longer parses as a number.
    text = re.sub(r"(?i)[A-Z]{0,2}\$", "", text)
    for ch in _CURRENCY_CHARS:
        text = text.replace(ch, "")
    text = re.sub(r"(?i)\b(SGD|USD|EUR|GBP|MYR|INR|RM)\b", "", text)

    # Normalise unicode dashes to ASCII minus.
    text = text.replace("−", "-").replace("–", "-").replace("—", "-")
    text = text.strip()

    if text.startswith("-"):
        negative = True
        text = text[1:]

    # The whole remaining string must BE a number - not merely contain one.
    #
    # This strictness is the point. A lenient "find any digits" search reads
    # "1 - 30 days overdue" as the amount 1 and throws the label away, which
    # is precisely the silent misread this application exists to prevent.
    # If letters survive the cleaning above, this is a label, not a figure.
    candidate = text.replace(" ", "")
    if not _STRICT_NUM_RE.match(candidate):
        return None

    try:
        amount = Decimal(candidate.replace(",", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None

    return -amount if negative else amount


def clean_label(value: Any) -> str:
    """Tidy an account description without changing its meaning."""
    if value is None:
        return ""
    text = str(value).replace("\n", " ").replace("\t", " ")
    text = re.sub(r"\s+", " ", text).strip()
    # Drop leading numbering like "1.2.3 " or "(a) "
    text = re.sub(r"^\(?[a-z0-9]{1,4}[.)]\s+", "", text, flags=re.IGNORECASE)
    return text.strip(" :-")


HEADER_WORDS = ("particular", "description", "account", "debit", "credit",
                "amount", "balance", "total", "s/n", "code", "note",
                "bucket", "ageing", "aging", "days", "item", "narration",
                "value", "ref", "category", "classification", "opening",
                "closing", "movement", "statement")

# Short header abbreviations, matched against a whole cell rather than as a
# substring. "dr" cannot be a substring test - it appears inside "address",
# and a ledger column headed "Address" would then look like a debit column.
HEADER_TOKENS = {"dr", "cr", "dr.", "cr.", "fs", "a/c", "ac", "gl",
                 "qty", "bal", "b/f", "c/f", "yr", "cy", "py"}


TOTAL_LABELS = {"total", "totals", "grand total", "sub total", "subtotal",
                "sub-total", "net total", "balance c/f", "balance b/f",
                # Opening and closing balances are a spreadsheet's own
                # bookkeeping, not accounts. A closing balance is the
                # balancing plug: import it and the trial balance appears to
                # balance perfectly while hiding the very difference the
                # auditor needs to see.
                "opening bal", "closing bal", "opening balance",
                "closing balance", "opening bal.", "closing bal.",
                "bal b/f", "bal c/f", "brought forward", "carried forward",
                "b/f", "c/f", "balance brought forward",
                "balance carried forward", "net movement", "difference",
                # A printed Profit & Loss or Balance Sheet carries computed
                # subtotals between its accounts. They are results, not
                # accounts, and importing one counts its accounts a second
                # time: a real engagement took in Gross Profit, Net Profit
                # and Net Assets as if each were a balance.
                #
                # "Current Year Earnings" is the year's result wearing an
                # equity account's name. Take a Profit & Loss and a Balance
                # Sheet together and the profit arrives twice - once as
                # revenue less expenses, once as this line - which leaves the
                # trial balance short by exactly one year's profit. Retained
                # Earnings is NOT here: that is the opening balance, a real
                # account, and dropping it would leave equity short.
                "current year earnings", "current period earnings",
                "current year profit", "profit and loss account",
                "gross profit", "gross loss", "net profit", "net loss",
                "net income", "operating profit", "operating loss",
                "net assets", "net liabilities", "net current assets",
                "profit before tax", "profit after tax",
                "profit before taxation", "profit after taxation",
                "earnings before tax", "ebit", "ebitda"}


def looks_like_total_label(label) -> bool:
    """True if a row is a document's own total rather than an account.

    Source documents carry their own totals. Those rows are useful to the
    auditor as a cross-check, but adding them to the statements would count
    every figure twice - and including them in the debit/credit reconciliation
    doubles both sides, which looks balanced while showing nonsense amounts.
    """
    text = (str(label or "")).strip().lower().rstrip(":").strip()
    if not text:
        return False
    return text in TOTAL_LABELS or text.startswith((
        "total ", "sub total ", "subtotal ",
        # "Profit for the year", "Net profit/(loss)", "Loss before tax" and
        # the rest of the ways a statement writes its own result.
        "profit for the", "loss for the", "profit before", "profit after",
        "loss before", "loss after", "net profit", "net loss", "gross profit",
        "gross loss", "net assets", "net current assets"))


def looks_like_header(cells: list) -> bool:
    """True if a row reads like a column header rather than data.

    Counts full words found anywhere in the row, plus short abbreviations
    matched against a whole cell. Client workbooks routinely head their
    columns "Dr." and "Cr." rather than "Debit" and "Credit"; without the
    abbreviations such a header is not recognised at all, and every column
    then has to be guessed by position.
    """
    joined = " ".join(str(c or "").lower() for c in cells)
    has_words = sum(1 for w in HEADER_WORDS if w in joined)
    has_words += sum(1 for c in cells
                     if str(c or "").strip().lower() in HEADER_TOKENS)
    has_numbers = any(parse_amount(c) is not None for c in cells)
    filled = sum(1 for c in cells if str(c or "").strip())
    return has_words >= 2 and not has_numbers and filled >= 2


def find_header_row(rows: list, scan: int = 15):
    """Locate the real header row, returning (index, cells) or (None, None).

    Picking the first row that merely *looks* like a header is not enough.
    A title block line such as "All amounts in SGD" trips the keyword test
    ("amount", "sgd") while the actual column header sits two rows below it,
    which shifts every subsequent row and turns the real header into a data
    row. So a candidate only counts if a row beneath it actually carries
    numbers - i.e. it genuinely heads a table.
    """
    for index, row in enumerate(rows[:scan]):
        cells = list(row)
        if not looks_like_header(cells):
            continue
        for follower in rows[index + 1:index + 6]:
            if any(parse_amount(c) is not None for c in follower):
                return index, cells
    return None, None


# --------------------------------------------------------------------------
# Confidence scoring
# --------------------------------------------------------------------------

def score_row(row: ExtractedRow, engine: str, threshold: float = 0.80) -> ExtractedRow:
    """Assign a confidence to one row and flag it if it needs an auditor's eye.

    Starts from the engine's base reliability, then applies penalties for the
    specific things that indicate a misread.
    """
    score = ENGINE_CONFIDENCE.get(engine, 0.7)

    # An AI-supplied per-row confidence overrides the engine baseline.
    if engine in AI_ENGINES and row.confidence < 1.0:
        score = row.confidence

    has_value = any(v is not None for v in (row.amount, row.debit, row.credit))

    if not has_value:
        score -= 0.20
    if not (row.label or "").strip():
        score -= 0.15
    if len((row.label or "").strip()) < 3 and has_value:
        score -= 0.10
    # A label that is mostly digits usually means columns got misaligned.
    label_text = (row.label or "").replace(" ", "")
    if label_text and sum(c.isdigit() for c in label_text) / len(label_text) > 0.6:
        score -= 0.15

    score = max(0.0, min(1.0, score))
    row.confidence = round(score, 3)
    row.needs_review = score < threshold
    return row


def reconcile_trial_balance(rows: list) -> Optional[dict]:
    """Check that debits equal credits.

    Returns a summary dict when the file carries debit/credit columns, so the
    review screen can show the auditor whether the trial balance actually
    balances. Returns None when the document isn't a debit/credit listing.
    """
    accounts = [r for r in rows if not looks_like_total_label(
        getattr(r, "label", None))]
    debits = [r.debit for r in accounts if r.debit is not None]
    credits = [r.credit for r in accounts if r.credit is not None]
    if not debits and not credits:
        return None

    total_debit = sum(debits, Decimal("0"))
    total_credit = sum(credits, Decimal("0"))
    difference = total_debit - total_credit
    return {
        "total_debit": total_debit,
        "total_credit": total_credit,
        "difference": difference,
        "balanced": abs(difference) < Decimal("0.01"),
    }
