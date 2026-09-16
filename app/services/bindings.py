"""Figures for library 2.x note tables, one row binding at a time.

Every row of a library 2.x table names where its figure comes from - a line
code, a sum of line codes, last year's balance, a field on a source document,
or a person. This resolves those tokens for one engagement, and builds the
table from them.

THE RULE IT KEEPS. A figure nobody can source is not nil. A row whose token
cannot be resolved - a fixed asset register not supplied, a MANUAL row not yet
answered, a total the library does not say how to compute - is marked HELD,
with the reason, and prints as "Incomplete". It never prints a dash: a dash in
a set of accounts tells the reader the balance is zero.

    token                      resolves to
    BS-TR, PL-STAFF ...        the accounts categorised to that line code
    PL-PBT, BS-TE ...          the generated statement line (no account has it)
    SUM:a+b                    the sum; held if any part is held
    PRIOR:x                    x, one year earlier
    FI:assets / FI:liabilities the financial instrument lines on that side
    DOC:total                  the total as its source states it (see _doc_total)
    EACH:a+b                   one row per line code carrying a balance
    PERACCOUNT:code            one row per trial balance account on that line
    PERCLASS:FAR:field         one row per class in the register (held for now)
    STATIC                     a label row, no figure
    MANUAL, CLIENT, FIRM, MEMO held: a person or record supplies it
    FAR:, AGED:, TAX:, REG:,   held: the source document is not read yet
    LOAN:, GL:, BANK:, PRIORFS:

THE ENGINE PERFORMS NO ARITHMETIC. From library 3.0 every printed figure is
taken from a source exactly as that source states it, totals included: the
engine does not add rows to produce a total, and does not add them to check
one. So there is no CALC here. A total row (DOC:total) is read from the
figure its own source already states - for rows drawn from the trial balance
that is the line on the face of the statements, which AuditMate built from
the same approved trial balance, added once, outside the notes. Where no
source states it, the row is held for the preparer rather than computed. The
arithmetic checks live on the Preparer checks sheet and are run by a person.

Figures are presented the way the statement shows them: assets and expenses
debit-positive, liabilities, equity and income credit-positive. A contra
balance (a loss allowance under current assets) comes out negative and prints
in brackets, so a "net" row adds up.

Nothing here calls the AI.
"""
import logging
from decimal import Decimal

from flask import g

from ..extensions import db
from ..models import FinancialStatement, NoteLibraryVersion, TrialBalanceAccount

log = logging.getLogger(__name__)

ZERO = Decimal("0")

# Statement lines no trial balance account is categorised to: totals and
# results the statements compute. Read from the generated statement instead.
COMPOSITE_LINES = {
    "PL-GP": ["gross_profit"],
    "PL-PBT": ["profit_before_tax"],
    "PL-PAT": ["profit_for_year"],
    "PL-OCI": ["other_comprehensive_income"],
    "PL-TCI": ["total_comprehensive_income"],
    "BS-TA": ["total_assets"],
    "BS-TE": ["total_equity"],
    "BS-TL": ["total_non_current_liabilities", "total_current_liabilities"],
    "BS-TEL": ["total_equity_and_liabilities"],
    # The face shows retained earnings after the year's result; the trial
    # balance account holds only the opening figure.
    "BS-RE": ["retained_earnings"],
}

CREDIT_SECTIONS = {
    "SOFP": {"equity", "non-current liabilities", "current liabilities",
             "liabilities"},
    "SOCI": {"revenue", "other income"},
}

DOCUMENT_TOKENS = {
    "PRIORFS": "last year's signed accounts",
    "FAR": "the fixed asset register",
    "AGED": "the aged receivables listing",
    "TAX": "the tax computation",
    "REG": "the company's registers",
    "LOAN": "the loan and lease schedules",
    "GL": "the general ledger",
    "BANK": "the bank confirmation",
}

PERSON_TOKENS = {
    "MANUAL": "To be entered by the preparer",
    "MEMO": "To be entered by the preparer",
    "CLIENT": "Comes from the client record",
    "FIRM": "Comes from a firm setting",
}

SINGLE_COLUMN = ("single amount column", "amount per period")


class Held:
    """A figure that cannot be stated yet, and why."""

    __slots__ = ("reason", "whole_year", "blocking")

    def __init__(self, reason, whole_year=False, blocking=True):
        self.reason = reason
        # True when nothing at all is known about that year - not one
        # figure, but the whole column.
        self.whole_year = whole_year
        # False for a figure the library says a note can be issued
        # without: the unutilised tax losses carried forward, the
        # unabsorbed capital allowances. A company with none of those is
        # not missing a disclosure, so the row is left out rather than
        # printed as Incomplete, which would hold finished accounts.
        self.blocking = blocking

    def __repr__(self):
        return f"<Held {self.reason}>"


def _is_held(value):
    return isinstance(value, Held)


# --------------------------------------------------------------------------
# The engagement's figures by line code
# --------------------------------------------------------------------------

def _version_for(financial_year):
    version = None
    if financial_year.library_version_id:
        version = db.session.get(NoteLibraryVersion,
                                 financial_year.library_version_id)
    if version is None or not version.sheet("Statement lines"):
        version = next((v for v in NoteLibraryVersion.query
                        .order_by(NoteLibraryVersion.imported_at.desc()).all()
                        if v.sheet("Statement lines")), None)
    return version


class Figures:
    """Everything one engagement's tables resolve against, loaded once."""

    def __init__(self, financial_year):
        from . import line_codes

        self.financial_year = financial_year
        version = _version_for(financial_year)
        self.lines = {}
        for row in (version.sheet("Statement lines") if version else []):
            code = row.get("Line code")
            if code:
                self.lines[code] = row
        self.categories = line_codes.load_categories()
        # Codes an account can carry - anything else is a computed line.
        self.account_codes = {c for spec in self.categories.values()
                              for c in (spec.get("codes") or [])}
        self._periods = {}

    # -- presentation -------------------------------------------------------

    def label(self, code):
        return (self.lines.get(code) or {}).get("Line label") or code

    def _credit_positive(self, code):
        row = self.lines.get(code) or {}
        statement = row.get("Statement")
        section = (row.get("Section") or "").strip().lower()
        return section in CREDIT_SECTIONS.get(statement, set())

    def present(self, code, debit_positive):
        return -debit_positive if self._credit_positive(code) else debit_positive

    # -- which year ---------------------------------------------------------

    def year(self, offset):
        """The engagement `offset` years back, or None if there is none."""
        from .outward import previous_year

        year = self.financial_year
        for _ in range(offset):
            if year is None:
                return None
            year = previous_year(year)
        return year

    def _code_of(self, account):
        """The account's line code, or the only one its line allows."""
        if account.line_code:
            return account.line_code
        options = (self.categories.get(account.standard_key) or {}).get("codes") or []
        return options[0] if len(options) == 1 else None

    def _from_accounts(self, accounts, prior_columns=False):
        """({code: debit-positive total}, {code: Held}, {code: [account ids]})."""
        totals, held, ids = {}, {}, {}
        for account in accounts:
            if prior_columns:
                if account.prior_debit is None and account.prior_credit is None:
                    continue
                net = (Decimal(str(account.prior_debit or 0))
                       - Decimal(str(account.prior_credit or 0)))
            else:
                net = (Decimal(str(account.debit or 0))
                       - Decimal(str(account.credit or 0)))
            if not account.standard_key:
                continue
            code = self._code_of(account)
            if code is None:
                if net:
                    reason = (f"{account.account_name} has no notes category "
                              f"chosen yet")
                    for option in ((self.categories.get(account.standard_key)
                                    or {}).get("codes") or []):
                        held.setdefault(option, Held(reason))
                continue
            totals[code] = totals.get(code, ZERO) + net
            ids.setdefault(code, []).append(account.id)
        return totals, held, ids

    def period(self, offset):
        """{"totals", "held", "ids", "statement", "missing"} for one year."""
        if offset in self._periods:
            return self._periods[offset]

        result = {"totals": {}, "held": {}, "ids": {}, "statement": {},
                  "missing": None}
        this_year = self.financial_year

        if offset == 0:
            accounts = TrialBalanceAccount.query.filter_by(
                financial_year_id=this_year.id).all()
            result["totals"], result["held"], result["ids"] = (
                self._from_accounts(accounts))
            result["statement"] = self._statement(this_year, previous=False)
        else:
            self._load_earlier(offset, result)

        self._periods[offset] = result
        return result

    def _load_earlier(self, offset, result):
        # A year before a first period since incorporation: nothing existed,
        # so every opening balance is genuinely nil.
        for step in range(offset):
            year = self.year(step)
            if year is not None and year.is_first_year:
                result["nil"] = True
                return

        engagement = self.year(offset)
        if engagement is not None:
            accounts = TrialBalanceAccount.query.filter_by(
                financial_year_id=engagement.id).all()
            if accounts:
                totals, held, ids = self._from_accounts(accounts)
                result["totals"], result["held"], result["ids"] = totals, held, ids
                result["statement"] = self._statement(engagement, previous=False)
                return

        # One year back only: this year's own sources of comparatives.
        if offset == 1:
            accounts = TrialBalanceAccount.query.filter_by(
                financial_year_id=self.financial_year.id).all()
            if any(a.prior_debit is not None or a.prior_credit is not None
                   for a in accounts):
                totals, held, ids = self._from_accounts(accounts,
                                                        prior_columns=True)
                result["totals"], result["held"], result["ids"] = totals, held, ids
                result["statement"] = self._statement(self.financial_year,
                                                      previous=True)
                return

            from . import prior_year

            figures, source = prior_year.balances(self.financial_year)
            if figures:
                self._from_key_totals(figures, source, result)
                result["statement"] = self._statement(self.financial_year,
                                                      previous=True)
                return

        where = ("last year" if offset == 1
                 else "the year before the comparative year")
        result["missing"] = f"Figures for {where} are not loaded"

    def _from_key_totals(self, figures, source, result):
        """Last year known only per statement line, not per account.

        A statement line's total can be put against a line code only when
        that line allows exactly one. Otherwise nobody knows how last year
        split between, say, deposits and prepayments, and every code under
        the line is held rather than guessed.
        """
        for key, amount in figures.items():
            options = (self.categories.get(key) or {}).get("codes") or []
            if not amount:
                continue
            if len(options) == 1:
                code = options[0]
                result["totals"][code] = result["totals"].get(code, ZERO) + amount
            else:
                reason = (f"Last year's figure for this line is not split into "
                          f"notes categories ({source})")
                for code in options:
                    result["held"].setdefault(code, Held(reason))

    @staticmethod
    def _statement(financial_year, previous):
        values = {}
        for statement in FinancialStatement.query.filter_by(
                financial_year_id=financial_year.id).all():
            for line in statement.lines:
                amount = line.amount_previous if previous else line.effective_amount
                if amount is not None:
                    values[line.line_key] = Decimal(str(amount))
        return values

    # -- one token ----------------------------------------------------------

    def keys_for_code(self, code):
        """The statement lines an account on this line code rolls up to."""
        return {key for key, spec in self.categories.items()
                if code in ((spec.get("codes") or []))}

    def statement_total(self, codes, offset):
        """What the statements already show for a set of line codes.

        This is where a total row on a trial balance table gets its figure:
        not by adding the rows of the note, but by reading the line the
        balance sheet or profit and loss prints - the same trial balance,
        added once, where it is already added. Held rather than guessed when
        the lines do not line up:

        A LINE THAT COVERS MORE THAN THE ROWS. If the statement line also
        carries a code the table has no row for, its figure is bigger than
        the rows beneath it, and printing it would state a total the note
        does not support. That is exactly the staff loans fault, and it is
        the library's own completeness rule - see uncovered_lines().
        """
        keys = set()
        for code in codes:
            found = self.keys_for_code(code)
            if not found:
                return Held(f"{self.label(code)} does not belong to a "
                            f"statement line, so no total can be read")
            keys |= found

        for key in sorted(keys):
            for other in (self.categories.get(key) or {}).get("codes") or []:
                if other in codes:
                    continue
                if any(self._carries(other, o) for o in (0, 1)):
                    return Held(f"Not in this table but on the same statement "
                                f"line: {self.label(other)} ({other})")

        period = self.period(offset)
        if period.get("nil"):
            return ZERO
        if period["missing"]:
            return Held(period["missing"], whole_year=True)
        total = ZERO
        for key in sorted(keys):
            if key not in period["statement"]:
                return Held(f"The statements do not show a figure for "
                            f"{key.replace('_', ' ')}")
            total += period["statement"][key]
        return total

    def _carries(self, code, offset):
        period = self.period(offset)
        if period.get("nil") or period["missing"]:
            return False
        return bool(period["totals"].get(code)) or code in period["held"]

    def accounts_on(self, code, offset=0):
        """(account id, name, balance) for each account on one line code."""
        from ..models import TrialBalanceAccount

        period = self.period(offset)
        ids = period["ids"].get(code) or []
        out = []
        for account in (TrialBalanceAccount.query
                        .filter(TrialBalanceAccount.id.in_(ids)).all()
                        if ids else []):
            net = (Decimal(str(account.debit or 0))
                   - Decimal(str(account.credit or 0)))
            out.append((account.id, account.account_name or "",
                        self.present(code, net)))
        return sorted(out, key=lambda row: abs(row[2]), reverse=True)

    def named_totals(self, offset=0):
        """{comparable account name: figure} for the period, or None.

        Only where the period is known account by account. A period read from
        a signed set gives line totals and no names, and a row cannot be
        matched to a name that is not there.
        """
        from ..models import TrialBalanceAccount

        period = self.period(offset)
        if period.get("nil") or period["missing"] or not period["ids"]:
            return None
        names = {}
        for code, ids in period["ids"].items():
            for account in (TrialBalanceAccount.query
                            .filter(TrialBalanceAccount.id.in_(ids)).all()):
                net = (Decimal(str(account.debit or 0))
                       - Decimal(str(account.credit or 0)))
                key = " ".join((account.account_name or "").split()).lower()
                names[key] = names.get(key, ZERO) + self.present(code, net)
        return names

    def resolve(self, token, offset):
        """A Decimal, a Held, or None for a label row with no figure."""
        token = (token or "").strip()

        if token == "STATIC" or not token:
            return None
        if token in PERSON_TOKENS:
            return Held(PERSON_TOKENS[token])
        if (token == "DOC:total" or token.startswith("EACH:")
                or token.startswith("PERACCOUNT:")):
            raise ValueError(f"{token} is resolved by the table, not alone")

        # One row per class in a register the engine cannot read yet.
        if token.startswith("PERCLASS:"):
            return self.resolve(token[len("PERCLASS:"):], offset)

        if token.startswith("PRIOR:"):
            return self.resolve(token[len("PRIOR:"):], offset + 1)

        if token.startswith("SUM:"):
            total = ZERO
            for part in token[len("SUM:"):].split("+"):
                value = self.resolve(part, offset)
                if _is_held(value):
                    return value
                total += value or ZERO
            return total

        if token.startswith("FI:"):
            side = token[len("FI:"):].strip().lower()
            codes = [code for code, row in self.lines.items()
                     if str(row.get("Financial instrument")).lower() == "yes"
                     and (self._credit_positive(code) == (side == "liabilities"))]
            if side not in ("assets", "liabilities"):
                return Held(f"{token} is not a known financial instrument group")
            return self.resolve("SUM:" + "+".join(codes), offset)

        prefix = token.split(":", 1)[0]
        if prefix in DOCUMENT_TOKENS:
            return self._document(prefix, token.split(":", 1)[-1], offset)

        return self._line_code(token, offset)

    def _document(self, token, field, offset):
        """A figure from a document: the one supplied, or why it is missing.

        Supplied means a person entered it for this engagement - see
        services/document_fields.py. Nothing is inferred and nothing
        defaults to nil: a field nobody has answered is held, because the
        company having no tax losses and nobody having looked yet are
        different statements and only the preparer knows which is true.

        The comparative column is not carried across from this year. Last
        year's tax computation is last year's document; if it is wanted
        it is entered against last year's engagement, where it belongs.
        """
        from . import document_fields

        if offset:
            year = self.year(offset)
            if year is None:
                return Held(f"There is no earlier engagement to take "
                            f"{token.lower()} figures from", whole_year=True)
        else:
            year = self.financial_year

        row = document_fields.value(year, token, field)
        if row is not None:
            return row.amount if row.amount is not None else ZERO

        blocking = document_fields.is_blocking(year, token, field)
        return Held(f"Needs {DOCUMENT_TOKENS[token]} ({field})",
                    blocking=blocking)

    def _line_code(self, code, offset):
        period = self.period(offset)
        if period.get("nil"):
            return ZERO
        if period["missing"]:
            return Held(period["missing"], whole_year=True)

        if code in COMPOSITE_LINES:
            keys = COMPOSITE_LINES[code]
            if all(k in period["statement"] for k in keys):
                return sum((period["statement"][k] for k in keys), ZERO)
            return Held(f"The {self.label(code).lower()} figure is not on the "
                        f"generated statements")

        if code in self.account_codes:
            if code in period["held"]:
                return period["held"][code]
            return self.present(code, period["totals"].get(code, ZERO))

        if code in self.lines:
            return Held(f"{self.label(code)} is not built from the trial "
                        f"balance yet")
        return Held(f"{code} is not a line code this library defines")

    def account_ids(self, token):
        """Trial balance accounts behind a token's current-year figure."""
        token = (token or "").strip()
        if token.startswith("PRIOR:") or token in PERSON_TOKENS:
            return []
        if token.startswith("SUM:"):
            ids = []
            for part in token[len("SUM:"):].split("+"):
                ids.extend(self.account_ids(part))
            return ids
        return list(self.period(0)["ids"].get(token, []))

    def codes_in(self, token):
        """Account-level line codes a token draws on, PRIOR included."""
        token = (token or "").strip()
        while token.startswith("PRIOR:"):
            token = token[len("PRIOR:"):]
        if token.startswith("SUM:") or token.startswith("EACH:"):
            return [c for part in token.split(":", 1)[1].split("+")
                    for c in self.codes_in(part)]
        return [token] if token in self.account_codes else []


def figures_for(financial_year):
    """One Figures per engagement per request - a report renders ninety notes."""
    try:
        cache = g.setdefault("_binding_figures", {})
    except RuntimeError:                              # no app context
        return Figures(financial_year)
    if financial_year.id not in cache:
        cache[financial_year.id] = Figures(financial_year)
    return cache[financial_year.id]


# --------------------------------------------------------------------------
# One table
# --------------------------------------------------------------------------

FIGURE_PREFIXES = ("BS-", "PL-", "CF-", "EQ-", "SUM:", "PRIOR:", "FI:",
                   "EACH:", "PERACCOUNT:", "DOC:")


def _is_figure(binding):
    return (binding or "").startswith(FIGURE_PREFIXES)


def _row(label, binding):
    return {"label": label, "binding": binding, "current": None,
            "previous": None, "bold": False, "rule": False, "ref": None}


def library_table(version_id, table_id):
    """The imported library table with this id, or None."""
    from ..models import NoteLibraryNote

    try:
        cache = g.setdefault("_library_tables", {})
    except RuntimeError:
        cache = {}
    if version_id not in cache:
        found = {}
        for note in NoteLibraryNote.query.filter_by(
                library_version_id=version_id).all():
            for piece in note.pieces or []:
                if piece.get("table_id"):
                    found[piece["table_id"]] = piece
        cache[version_id] = found
    return cache[version_id].get(table_id)


def build_table(spec, financial_year, statements=None):
    """A renderable table for one library table, or None if nothing to show.

    `spec` names the table - {"source": "bindings", "version_id", "table_id"}
    - and it is looked up at render time, like every other note table, so the
    figures follow the trial balance rather than the day the report was made.
    """
    table = library_table(spec.get("version_id"), spec.get("table_id")) or {}
    library_rows = table.get("rows") or []
    if not library_rows:
        return None

    figures = figures_for(financial_year)
    first_year = bool(financial_year.is_first_year)
    single_column = str(table.get("column_labels") or "").strip().lower() \
        .startswith(SINGLE_COLUMN)

    # The same figure named on two rows of one table would print it twice.
    counts = {}
    for row in library_rows:
        if _is_figure(row.get("binding")):
            counts[row["binding"]] = counts.get(row["binding"], 0) + 1

    rows = []
    for library_row in library_rows:
        label, binding = library_row.get("label"), (library_row.get("binding") or "").strip()

        if binding.startswith("EACH:"):
            for code in binding[len("EACH:"):].split("+"):
                row = _row(figures.label(code), code)
                _fill(row, code, figures, first_year)
                rows.append(row)
            continue

        # One row per trial balance account on the line, under the account's
        # own caption. The library used to bind this to the whole line, which
        # printed the line total once as a row and again as the total.
        if binding.startswith("PERACCOUNT:"):
            code = binding[len("PERACCOUNT:"):].strip()
            last_year = figures.named_totals(1) if not first_year else None
            for account_id, name, amount in figures.accounts_on(code):
                row = _row(name or figures.label(code), binding)
                row["current"] = amount
                if first_year:
                    row["previous"] = None
                elif last_year is None:
                    row["previous"] = Held(
                        "Last year is known only in total, so it cannot be "
                        "split by account")
                else:
                    row["previous"] = last_year.get(
                        " ".join(name.split()).lower(), ZERO)
                row["ids"] = [account_id]
                rows.append(row)
            if not any(r["binding"] == binding for r in rows):
                row = _row(label, binding)
                _fill(row, code, figures, first_year)
                rows.append(row)
            continue

        row = _row(label, binding)
        if binding == "DOC:total":
            row["bold"] = row["rule"] = True
        elif not single_column and binding != "STATIC":
            row["current"] = Held("This table has several columns per year, "
                                  "which is not laid out yet")
            row["previous"] = None if first_year else row["current"]
        elif counts.get(binding, 0) > 1:
            row["current"] = Held("The library binds more than one row of this "
                                  "table to the same figure")
            row["previous"] = None if first_year else row["current"]
        else:
            _fill(row, binding, figures, first_year)
        rows.append(row)

    totals = [i for i, row in enumerate(rows) if row["binding"] == "DOC:total"]
    for index in totals:
        _doc_total(rows, index, figures, first_year,
                   named=table.get("totals_agree_with")
                   if index == totals[-1] else None)

    shown = [row for row in rows if not _nil(row) and not _optional_gap(row)]

    # Shown only once something in it is a figure from the books. A table
    # made entirely of document fields and preparer answers waits for those.
    if not any(_is_figure(row["binding"])
               and any(isinstance(v, Decimal) and v for v in
                       (row["current"], row["previous"]))
               for row in shown):
        return _held_table(spec, table, rows, figures)

    for row in shown:
        if not row.get("ids"):
            row["ids"] = (figures.account_ids(row["binding"])
                          if _is_figure(row["binding"]) else [])
        if len(row["ids"]) == 1:
            row["ref"] = f"tb:{row['ids'][0]}"
        for column in ("current", "previous"):
            if _is_held(row[column]):
                row[f"held_{column}"] = row[column].reason
                row[column] = None
        # Kept, not dropped: the Preparer checks page asks where else in
        # the draft the same line code prints, and a rendered row is the
        # only place that is still known after EACH: and PERACCOUNT: have
        # expanded one library row into several. Renamed so nothing
        # downstream can mistake it for a token still to be resolved.
        row["from_binding"] = row.pop("binding")

    return {"heading": spec.get("heading"), "rows": shown,
            "columns": table.get("column_labels"),
            "table_id": table.get("table_id")}


def _held_table(spec, table, rows, figures):
    """A table with no figure from the books yet: incomplete, or not needed.

    Not needed when the note's own lines (the Statement lines sheet) carry no
    balance in either year - a company with no plant and equipment is not
    missing a fixed asset register. Otherwise, if any row waits on a document
    or an answer, the table is shown as one incomplete line naming what it
    needs, never as a grid of dashes and never silently dropped.
    """
    from . import conditions

    note_code = spec.get("note_code")
    if note_code:
        subjects = conditions.subject_codes(figures, note_code)
        if subjects and not any(conditions.carries_balance(figures, c)
                                for c in subjects):
            return None

    reasons = []
    for row in rows:
        for column in ("current", "previous"):
            value = row[column]
            if _is_held(value) and value.reason not in reasons:
                reasons.append(value.reason)
    if not reasons:
        return None
    return {"heading": spec.get("heading"), "rows": [],
            "held_table": reasons, "columns": table.get("column_labels"),
            "table_id": table.get("table_id")}


def _fill(row, binding, figures, first_year):
    row["current"] = figures.resolve(binding, 0)
    row["previous"] = None if first_year else figures.resolve(binding, 1)


def _optional_gap(row):
    """A row waiting only on a figure the library says is not required.

    Left out rather than printed as Incomplete. The library marks the
    unutilised tax losses and the unabsorbed capital allowances
    non-blocking because a company with none of them is not missing a
    disclosure - and a row that prints Incomplete holds the whole note,
    which would stop accounts that are finished.
    """
    values = [row.get("current"), row.get("previous")]
    held = [v for v in values if _is_held(v)]
    if not held or any(isinstance(v, Decimal) for v in values):
        return False
    return all(not v.blocking for v in held)


def _nil(row):
    """A figure row with nothing in either year - suppressed, per the library."""
    if not _is_figure(row["binding"]):
        return False
    current, previous = row["current"], row["previous"]
    # Nil this year, and last year not loaded at all: nothing to say about
    # the row yet. The rows that do carry a figure still show last year as
    # incomplete, so the gap stays visible.
    if (isinstance(current, Decimal) and not current
            and _is_held(previous) and previous.whole_year):
        return True
    if _is_held(current) or _is_held(previous):
        return False
    return not any((current, previous))


def _doc_total(rows, index, figures, first_year, named=None):
    """A total row: read from the source, never added up here.

    Where the rows above it come from the trial balance, the source that
    already states their total is the face of the statements - built once
    from the same approved trial balance. `named` is the library's own
    "Totals agree with" column, which says which lines the table's final
    total must equal; a subtotal partway down a table is read from the rows
    since the last total instead.

    Held, not guessed, when no source states it: when a row above comes from
    a document or a person rather than the books, and when the statement line
    covers a balance this table has no row for.
    """
    row = rows[index]
    previous = max([i for i in range(index) if rows[i]["binding"] == "DOC:total"],
                   default=-1)
    group = [r for r in rows[previous + 1:index]
             if r["binding"] not in ("STATIC", "DOC:total")]

    # The library names the lines a table's own total must equal. Some
    # entries are a sentence rather than a list - "every line flagged as a
    # financial asset" - and those fall back to the lines the rows above
    # actually draw on, which is the same set said another way.
    # A total the face of the statements does not show, because the face
    # does not split financial instruments out: trade payables there include
    # GST, which is not one. The library composes it instead - the same
    # FI:liabilities token its own by-category table uses.
    spoken = str(named or "").strip().lower()
    if "financial asset" in spoken or "financial liabilit" in spoken:
        side = "assets" if "financial asset" in spoken else "liabilities"
        for column, offset in (("current", 0), ("previous", 1)):
            if column == "previous" and first_year:
                continue
            row[column] = figures.resolve(f"FI:{side}", offset)
        return

    codes = []
    if named and str(named).strip().lower() not in ("no total", "-"):
        codes = [c.strip() for c in str(named).replace("+", " ").split()
                 if c.strip() in figures.lines]
    if not codes:
        for member in group:
            found = figures.codes_in(member["binding"])
            if not found:
                codes = []
                break
            codes.extend(found)

    for column, offset in (("current", 0), ("previous", 1)):
        if column == "previous" and first_year:
            continue
        if not codes:
            row[column] = Held("No document states this total; enter it")
            continue
        row[column] = figures.statement_total(codes, offset)


def uncovered_lines(specs, financial_year):
    """Balances a note should show and has no row for.

    The library's own completeness rule from version 3.5: a mapped account
    whose line has no row, no combined row and no total to belong to holds
    its note incomplete. It needs no arithmetic - it only asks whether a line
    has somewhere to print - which is why it stays the engine's job now that
    the checks have moved to the preparer.
    """
    from . import conditions

    figures = figures_for(financial_year)
    printed, note_codes = set(), set()
    for spec in specs or []:
        if spec.get("source") != "bindings":
            continue
        if spec.get("note_code"):
            note_codes.add(spec["note_code"])
        table = library_table(spec.get("version_id"), spec.get("table_id")) or {}
        for binding in table.get("row_bindings") or []:
            printed.update(figures.codes_in(binding))
            printed.update(c for c in str(binding).replace("+", " ").split()
                           if c in figures.lines)
        named = str(table.get("totals_agree_with") or "")
        printed.update(c for c in named.replace("+", " ").split()
                       if c in figures.lines)

    missing = []
    for note_code in sorted(note_codes):
        for code in conditions.subject_codes(figures, note_code):
            if code in printed or code not in figures.account_codes:
                continue
            if conditions.carries_balance(figures, code):
                missing.append(f"{figures.label(code)} ({code}) has a balance "
                               f"and no row in this note")
    return missing
