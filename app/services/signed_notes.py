"""Last year's figures at the grain the notes need, read from the notes of the
signed accounts.

The comparative column of a note is last year's figure per line: trade
receivables, other receivables, prepayments, deposits. The signed accounts'
BALANCE SHEET gives one line for all of them - "Trade and other receivables
39,953" - and the notes library splits that line into five codes, so every
one of them was held as Incomplete: "last year's signed accounts give this
line in total, and it is not split into notes categories".

The split is on file. It is in the signed accounts' own note 8, printed as
"Trade receivables - Third parties 62,966 / Amount due from a director
(29,577) / Prepayments 6,563". This reads those notes.

Trusted only where it can be proved. A note's rows are taken for a statement
line only when they add up to what the signed balance sheet says that line
was - 62,966 - 29,577 + 6,563 is 39,952 against 39,953, so the rows are
last year's split of that line. A note that does not add up is left alone,
and the cells stay Incomplete rather than take a figure nobody checked. The
same figure printed in a second note (cash appears in the cash note and again
in the financial instruments note) is taken once.

Deterministic: reads the PDF's own text, calls no model.
"""
import logging
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

log = logging.getLogger(__name__)

ZERO = Decimal("0")

# The token these are stored under. Not PRIORFS: that one is a preparer's
# typed reading of the signed set, keyed by statement line, and is also read
# by the comparison of last year's sources - a line code among those would
# look like a statement line that does not exist.
TOKEN = "PRIORNOTE"
PROVENANCE = "Signed accounts, note"

TOLERANCE = Decimal("2")            # the signed set's own rounding

_AMT = r"\(?-?\d[\d,]*(?:\.\d+)?\)?|[-–]"
_ROW = re.compile(rf"^(?P<label>.*[A-Za-z].*?)\s+(?P<a>{_AMT})\s+(?P<b>{_AMT})\s*$")
_NOTE_HEAD = re.compile(r"^(?P<no>\d{1,2})\.\s+(?P<title>[A-Z][^0-9].{2,})$")
_FURNITURE = ("brown rock", "notes to the financial", "for the financial year",
              "these notes form", "the accompanying")


def _amount(token):
    token = token.strip()
    if token in ("-", "–"):
        return ZERO
    negative = token.startswith("(") or token.startswith("-")
    value = Decimal(re.sub(r"[^\d.]", "", token) or "0")
    return -value if negative else value


def read_rows(path):
    """[{note, title, label, context, current, previous, total}] from the note
    pages of a signed set, in printed order. [] if the file cannot be read."""
    import pdfplumber

    rows = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            in_notes = False
            note = title = context = None
            for page in pdf.pages:
                lines = [l.strip() for l in (page.extract_text() or "").splitlines()
                         if l.strip()]
                head = " ".join(lines[:4]).lower()
                if "notes to the financial statements" in head:
                    in_notes = True
                if not in_notes:
                    continue
                for line in lines:
                    low = line.lower()
                    if low.startswith(_FURNITURE) or low.startswith("for the "):
                        continue
                    heading = _NOTE_HEAD.match(line)
                    if heading and not _ROW.match(line):
                        note = heading.group("no")
                        title = heading.group("title").strip()
                        context = None
                        continue
                    match = _ROW.match(line)
                    if not match:
                        # A caption inside a note ("Trade receivables",
                        # "Other receivables") that qualifies the dashed
                        # rows beneath it.
                        if (note and len(line) < 60
                                and not line.endswith((".", ":"))
                                and line[0].isupper()):
                            context = line
                        continue
                    label = match.group("label").strip()
                    bulleted = label.startswith("-")
                    label = label.lstrip("- ").strip()
                    full = f"{context} {label}".strip() if bulleted and context else label
                    rows.append({
                        "note": note, "title": title, "label": full,
                        "current": _amount(match.group("a")),
                        "previous": _amount(match.group("b")),
                        "total": low.startswith("total") or label.lower().startswith("total"),
                    })
    except Exception:                                        # noqa: BLE001
        log.exception("Could not read the notes of %s", path)
        return []
    return rows


def _pseudo_account(label, key):
    return SimpleNamespace(account_name=label, standard_key=key,
                           debit=None, credit=None)


def plan(financial_year, rows=None):
    """{code: (amount as printed, provenance)} - what the notes prove.

    Nothing is written. `rows` lets a test hand in parsed rows.
    """
    from . import line_codes, prior_year
    from .classify import is_credit_balance
    from .mapping import match_label

    signed = _signed_document(financial_year)
    if signed is None:
        return {}
    if rows is None:
        path = Path(signed.storage_path)
        if not path.exists() or path.suffix.lower() != ".pdf":
            return {}
        rows = read_rows(path)
    if not rows:
        return {}

    face = prior_year.sources(financial_year).get("signed_accounts") or {}
    if not face:
        return {}

    categories = line_codes.load_categories()
    known = line_codes.known_codes(financial_year)

    # Each note's rows, as (statement line, line code, row).
    by_note = {}
    for row in rows:
        if row["total"] or not row["note"]:
            continue
        rule = match_label(row["label"], financial_year.customer_id)
        if not rule:
            continue
        key = rule["line_key"]
        code, _source = line_codes.propose(
            _pseudo_account(row["label"], key), categories, None, known)
        if not code:
            continue
        by_note.setdefault(row["note"], []).append((key, code, row))

    accepted, done_keys = {}, set()
    for note in sorted(by_note, key=int):
        members = by_note[note]
        keys = {key for key, _code, _row in members}
        if keys & done_keys:
            continue                     # already taken from an earlier note

        # The note as a whole against the signed balance sheet. The face can
        # hold several of our lines under one caption ("Trade and other
        # receivables" is all of receivables, deposits and prepayments), so
        # the note is proved against the lines it covers together, not one
        # by one.
        printed = sum(((-1 if is_credit_balance(key) else 1) * row["current"]
                       for key, _code, row in members), ZERO)
        shown = sum((face.get(key, ZERO) for key in keys), ZERO)
        if not shown or abs(printed - shown) > TOLERANCE:
            continue

        done_keys |= keys
        title = members[0][2]["title"]
        by_code = {}
        for _key, code, row in members:
            by_code[code] = by_code.get(code, ZERO) + row["current"]
        # A code those lines allow that the note does not print was nil.
        for key in keys:
            for code in line_codes.allowed_codes(key, categories):
                if known is None or code in known:
                    by_code.setdefault(code, ZERO)
        for code, amount in by_code.items():
            accepted[code] = (amount, f"{PROVENANCE} {note}, {title}")

    _from_trial_balance(financial_year, face, done_keys, accepted,
                        categories, known)
    return accepted


def _from_trial_balance(financial_year, face, done_keys, accepted,
                        categories, known):
    """The split of a line from this year's trial balance's last-year column.

    The trial balance carries last year per account, which is the grain the
    notes need; the signed accounts are what was actually reported. Used only
    where the two agree - the account-level figures for a statement line add
    up to the signed set's figure for it - so the split is one the signed
    accounts vouch for. Where they differ (the books and the signed set do,
    on staff medical and a few others) nothing is taken and the cells stay
    Incomplete, rather than print a split that contradicts what was filed.
    """
    from ..models import TrialBalanceAccount
    from . import bindings

    figures = bindings.Figures(financial_year)
    by_key = {}
    for account in TrialBalanceAccount.query.filter_by(
            financial_year_id=financial_year.id).all():
        if not account.standard_key:
            continue
        if account.prior_debit is None and account.prior_credit is None:
            continue
        by_key.setdefault(account.standard_key, []).append(account)

    # Lines the signed accounts print as ONE caption but the books hold as
    # several: "Other income" is other revenue, interest income and rebates.
    grouped = [{"other_income", "interest_income", "iras_rebate"}]
    clusters, used = [], set()
    for group in grouped:
        members = group & (set(by_key) | set(face))
        if len(members) > 1:
            clusters.append(members)
            used |= members
    clusters += [{key} for key in by_key if key not in used]

    for cluster in clusters:
        keys = {k for k in cluster if k in by_key}
        if not keys or keys & done_keys:
            continue
        shown = sum((face.get(k, ZERO) for k in cluster), ZERO)
        if not shown:
            continue
        accounts = [a for k in keys for a in by_key[k]]
        net = sum((Decimal(str(a.prior_debit or 0))
                   - Decimal(str(a.prior_credit or 0)) for a in accounts), ZERO)
        if abs(net - shown) > TOLERANCE:
            continue
        options = {k: [c for c in (categories.get(k) or {}).get("codes") or []
                       if known is None or c in known] for k in cluster}
        by_code, ok = {}, True
        for account in accounts:
            opts = options.get(account.standard_key) or []
            code = account.line_code or (opts[0] if len(opts) == 1 else None)
            if code is None:
                ok = False
                break
            # As the library prints the line: credit-positive lines (income,
            # liabilities, equity) as positive figures.
            sign = -1 if figures._credit_positive(code) else 1
            amount = (Decimal(str(account.prior_debit or 0))
                      - Decimal(str(account.prior_credit or 0)))
            by_code[code] = by_code.get(code, ZERO) + sign * amount
        if not ok:
            continue
        # One code carries the whole caption: state the signed set's own
        # figure, not the books' cents, so the note and the statement agree.
        carrying = [c for c, amount in by_code.items() if amount]
        if len(carrying) == 1:
            code = carrying[0]
            by_code[code] = (-1 if figures._credit_positive(code) else 1) * shown
        for k in cluster:
            for code in options.get(k) or []:
                by_code.setdefault(code, ZERO)
        for code, amount in by_code.items():
            accepted.setdefault(
                code, (amount, "Last year's trial balance column, "
                               "agreeing with the signed accounts"))
        done_keys |= cluster


_HOLDING = re.compile(
    r"^(?P<name>[A-Z][A-Za-z'\u2019.\- ]{2,}?)\s+(?P<a>\d[\d,]*)\s+(?P<b>\d[\d,]*)$")


def read_shareholdings(path):
    """[(director, shares at the beginning, shares at the end)] from the
    directors' statement of a signed set, or []."""
    import pdfplumber

    found = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            armed = False
            for page in pdf.pages[:6]:
                for line in (page.extract_text() or "").splitlines():
                    line = line.strip()
                    low = line.lower()
                    if "name of director" in low:
                        armed = True
                        continue
                    if armed and re.match(r"^5\.\s", line):
                        return found
                    match = _HOLDING.match(line) if armed else None
                    if match:
                        found.append((match.group("name").strip(),
                                      _amount(match.group("a")),
                                      _amount(match.group("b"))))
    except Exception:                                        # noqa: BLE001
        log.exception("Could not read the shareholdings of %s", path)
    return found


def _signed_document(financial_year):
    for document in financial_year.documents:
        if (document.category == "signed_accounts"
                and document.review_status == "verified"):
            return document
    return None


def fill(financial_year):
    """Store what plan() proves as this year's last-year note figures.

    Replaces its own earlier rows and nothing else. Returns how many codes
    were stored. Safe to run on every report build: no signed set, an
    unreadable one, or notes that do not add up store nothing.
    """
    from ..extensions import db
    from ..models import DocumentFigure

    figures = plan(financial_year)
    DocumentFigure.query.filter_by(financial_year_id=financial_year.id,
                                   token=TOKEN).delete()
    _fill_shareholdings(financial_year)
    from . import share_capital
    share_capital.fill(financial_year)
    for code, (amount, where) in figures.items():
        db.session.add(DocumentFigure(
            financial_year_id=financial_year.id, token=TOKEN, field=code,
            scope="", member="", amount=amount, found_at=where[:255]))
    db.session.flush()
    return len(figures)


def _fill_shareholdings(financial_year):
    """The directors' holdings from the signed set, where none are on file.

    The library reads them from the share register or the business profile;
    a client's signed accounts print the same table. Never over a figure a
    person entered.
    """
    from ..extensions import db
    from ..models import DocumentFigure

    existing = DocumentFigure.query.filter_by(
        financial_year_id=financial_year.id, token="REG").first()
    signed = _signed_document(financial_year)
    if existing is not None or signed is None:
        return 0
    path = Path(signed.storage_path)
    if not path.exists() or path.suffix.lower() != ".pdf":
        return 0
    holdings = read_shareholdings(path)
    for name, opened, closed in holdings:
        for field, amount in (("shares_open", opened), ("shares_close", closed)):
            db.session.add(DocumentFigure(
                financial_year_id=financial_year.id, token="REG", field=field,
                scope="", member=name, amount=amount,
                found_at="Signed accounts, directors' statement"))
    db.session.flush()
    return len(holdings)
