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

SINGLE_COLUMN = ("single amount column", "amount per period",
                 "current year")

# A movement table presented one column per class of asset, with a Total
# column beside them. The library says so in the table's own column
# labels: "By class of asset, as mapped. A Total column is always
# presented."
BY_CLASS_COLUMNS = "by class"

# The Total column of a by-class table is an ordinary column, filled from
# a source like every other. It is NOT the classes added up: the engine
# performs no arithmetic, and the register prints its own total row for a
# person to take.
TOTAL_COLUMN = "Total"


class Held:
    """A figure that cannot be stated yet, and why."""

    __slots__ = ("reason", "whole_year", "blocking",
                "token", "field", "scope", "member")

    def __init__(self, reason, whole_year=False, blocking=True,
                token=None, field=None, scope="", member=""):
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
        # Set only where this hold names an exact document figure - the
        # token and field services/document_fields.save() takes. A note
        # left held for a structural reason (no earlier engagement, a
        # code the library does not define, a question still waiting on
        # the preparer) carries none of these, and stays read-only: it
        # names nothing a single figure could answer.
        self.token = token
        self.field = field
        self.scope = scope
        self.member = member

    @property
    def editable(self):
        """True where a preparer could answer this hold on the spot.

        Only a document-sourced figure qualifies - the token and field
        are exactly what document_fields.save() needs, and saving through
        it writes the same row the Figures page would, so answering a
        note's Incomplete cell and answering the Figures page are the
        same act rather than two that can disagree.
        """
        return self.token is not None and self.field is not None

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

    def _signed_corrections(self, result):
        """Lay a preparer's reading of the signed set over last year.

        The library's PRIORFS wildcard, one line at a time: somebody opens
        last year's filed accounts, sees that depreciation was 286 where the
        books say 230, and types the 286.

        An overlay, never a replacement. A person who retypes one line has
        said nothing about the other seventy-nine, and swapping the whole
        period for their handful of figures would read every line they did
        not mention as absent - which this engine turns into nil, stating
        that last year's revenue was nought. So the period keeps whatever
        detail it had and only the lines actually typed move.

        A line the categories split between several codes is held rather
        than assigned: the signed accounts printed one figure for deposits
        and prepayments together, and nobody knows how it divided.

        The figure is typed AS PRINTED - 487,419 read off last year's
        income statement, not minus 487,419 - because asking anyone to
        negate a revenue figure before typing it invites the one mistake
        nobody would catch. The statement values share that convention and
        take it unchanged; the account totals are debit-positive, so they
        get the same flip present() applies on the way back out, which
        leaves the printed figure exactly where it started.
        """
        typed = {row.field: row.amount for row in
                 self._entered_rows() if row.amount is not None}
        if not typed:
            return

        for key, amount in typed.items():
            amount = Decimal(str(amount))
            result["statement"][key] = amount
            options = (self.categories.get(key) or {}).get("codes") or []
            if len(options) == 1:
                code = options[0]
                result["totals"][code] = self.present(code, amount)
                result["held"].pop(code, None)
                result["ids"].pop(code, None)
            else:
                reason = ("Last year's signed accounts give this line in "
                          "total, and it is not split into notes categories")
                for code in options:
                    result["held"][code] = Held(reason)
                    result["totals"].pop(code, None)
                    result["ids"].pop(code, None)

    def _note_figures(self, result):
        """Last year at line-code grain, read from the signed accounts' notes.

        Where the signed balance sheet gives one figure for a line the library
        splits, the signed accounts' own note gives the split - see
        services/signed_notes. Only codes that note PROVED are here (its rows
        add up to the signed balance sheet), so a code present is a settled
        figure and one absent stays exactly as it was: held.
        """
        from ..models import DocumentFigure

        for row in DocumentFigure.query.filter_by(
                financial_year_id=self.financial_year.id, token="PRIORNOTE",
                scope="").all():
            if row.amount is None:
                continue
            code = row.field
            result["totals"][code] = self.present(code, Decimal(str(row.amount)))
            result["held"].pop(code, None)
            result["ids"].pop(code, None)

    def _entered_rows(self):
        from ..models import DocumentFigure

        return DocumentFigure.query.filter_by(
            financial_year_id=self.financial_year.id, token="PRIORFS",
            scope="").all()

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
            # Whatever last year came from, a preparer's reading of the
            # signed accounts sits on top of it. Applied here rather than
            # inside _load_earlier because that function returns from four
            # different branches and every one of them can be corrected.
            if offset == 1 and not result.get("nil"):
                self._signed_corrections(result)
                self._note_figures(result)

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

        # LAST YEAR'S SIGNED ACCOUNTS FIRST, from library 3.5.
        #
        # The comparative column is not what the books say about last year.
        # It is what was reported and filed, and the two are allowed to
        # differ - on the test client depreciation is 286 in the signed set
        # and 230 in the trial balance. So a whole signed set outranks every
        # trial balance, including our own previous engagement.
        #
        # The price is detail: a signed set gives line totals and no account
        # names, so a row that splits a line by account (PERACCOUNT)
        # correctly reports last year as known only in total rather than
        # inventing a split the signed accounts never made. That price is
        # worth paying for a whole signed set and is NOT worth paying for a
        # correction to one line - see _signed_corrections below.
        if offset == 1:
            from . import prior_year

            available = prior_year.sources(self.financial_year)
            if "signed_accounts" in available:
                self._from_key_totals(
                    available["signed_accounts"],
                    prior_year.SOURCE_LABELS["signed_accounts"], result)
                result["statement"] = self._statement(
                    self.financial_year, previous=True)
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

        # Administrative expenses are printed as ONE line in a signed set.
        # The codes for staff costs, depreciation, GST and the rest are folded
        # into it, not absent from it - and absent was read as nil, so last
        # year's employee benefits printed as a dash beside a signed note that
        # says 453,821. Held: it is known only inside the total.
        if any(figures.get(key) for key in self.keys_for_code("PL-ADM")):
            reason = ("Last year's administrative expenses are one figure in "
                      f"the signed accounts, not split by kind ({source})")
            for code, row in self.lines.items():
                if (str(row.get("Statement")) == "SOCI"
                        and (row.get("Section") or "").strip().lower()
                        == "expenses"
                        and code != "PL-ADM" and code in self.account_codes
                        and code not in result["totals"]
                        and code not in result["held"]):
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
                    # The label alone. The code used to follow it in
                    # brackets, which told a developer which binding and
                    # told a preparer nothing they could act on.
                    return Held(f"Not in this table but on the same statement "
                                f"line: {self.label(other)}")

        period = self.period(offset)
        if period.get("nil"):
            return ZERO
        if period["missing"]:
            return Held(period["missing"], whole_year=True)
        # Other income, interest income and the IRAS rebate sit on the
        # statements the way the trial balance holds them - a credit is a
        # negative - unlike revenue, which is flipped. A note prints them as
        # income, positive.
        flipped = {"other_income", "interest_income", "iras_rebate"}
        shown = [k for k in keys if k in period["statement"]]
        total = ZERO
        for key in sorted(keys):
            if key not in period["statement"]:
                # A signed set has no line for what it did not earn: with a
                # sibling income line present, this one is nil, not unknown.
                if offset and key in flipped and shown:
                    continue
                return Held(f"The statements do not show a figure for "
                            f"{key.replace('_', ' ')}")
            amount = period["statement"][key]
            total += -amount if key in flipped else amount
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

    def resolve(self, token, offset, scope=""):
        """A Decimal, a Held, or None for a label row with no figure.

        `scope` is the table the row sits in. It matters only for a
        document field whose name repeats across notes - the same
        PRIORFS:cost_open_py is three different balances - and is ignored
        by every other token.
        """
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
            return self.resolve(token[len("PERCLASS:"):], offset, scope)

        if token.startswith("PRIOR:"):
            return self.resolve(token[len("PRIOR:"):], offset + 1, scope)

        # "Closing balance of one named mapped line", for a row that names a
        # line other than the note's own - BALANCE:PL-ADM on the last row of
        # the administrative expenses note. Not implemented before, so that
        # row read "PL-ADM is not a line code this library defines".
        # Last year's is the signed total LESS the rows above it, which are
        # not all known, so it is held rather than guessed.
        if token.startswith("BALANCE:"):
            if offset:
                return Held("Last year's is the signed total less the rows "
                            "above it, and those are not all known")
            return self.section_total(token[len("BALANCE:"):].strip(), offset)

        if token.startswith("SUM:"):
            total = ZERO
            for part in token[len("SUM:"):].split("+"):
                value = self.resolve(part, offset, scope)
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
            return self.resolve("SUM:" + "+".join(codes), offset, scope)

        prefix = token.split(":", 1)[0]
        if prefix in DOCUMENT_TOKENS:
            return self._document(prefix, token.split(":", 1)[-1], offset,
                                  scope)

        return self._line_code(token, offset)

    def _document(self, token, field, offset, scope=""):
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

        row = document_fields.value(year, token, field, scope)
        if row is not None:
            return row.amount if row.amount is not None else ZERO

        blocking = document_fields.is_blocking(year, token, field, scope)
        return Held(f"Needs {DOCUMENT_TOKENS[token]} ({field})",
                    blocking=blocking, token=token, field=field, scope=scope)

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

    def section_total(self, code, offset):
        """The whole line a code stands for, where the code is a heading.

        PL-ADM is both the code of an account with nothing more specific to
        say and the name of the whole "Administrative expenses" caption; the
        library's balancing row means the caption. It is every account whose
        code sits in the statement's Expenses section - staff, depreciation,
        GST and the rest - so that the rows above it and the balancing row
        add up to the line they claim to explain.
        """
        if code != "PL-ADM":
            return self.resolve(code, offset)
        if offset:
            return Held("Last year's is the signed total less the rows "
                        "above it, and those are not all known")
        members = [c for c, row in self.lines.items()
                   if str(row.get("Statement")) == "SOCI"
                   and (row.get("Section") or "").strip().lower() == "expenses"
                   and c in self.account_codes]
        total = ZERO
        for member in members:
            value = self.resolve(member, offset)
            if _is_held(value):
                return value
            total += value or ZERO
        return total

    def account_ids(self, token):
        """Trial balance accounts behind a token's current-year figure."""
        token = (token or "").strip()
        if token.startswith("BALANCE:"):
            return self.account_ids(token[len("BALANCE:"):])
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


def by_class_table(spec, financial_year):
    """A movement table laid out one column per class of asset.

    The fixed asset, investment property and intangibles notes are
    presented this way, and until now the engine held them whole - "several
    columns per year, which is not laid out yet". This is that layout.

    WHERE EACH CELL COMES FROM. A row bound to a register field or to last
    year's signed accounts is stated per class, because both documents are
    kept that way, and its Total column is stated too - the register prints
    its own total row and a person takes it. Nothing is added up here.

    A row bound to a line code - the net carrying amount at each year end -
    is a different case. The trial balance carries one figure for the whole
    line and the library gives no per-class field for it, so those rows
    fill the Total column and leave the class columns EMPTY rather than
    holding them. An empty cell where the library never promised a figure
    is honest; "Incomplete" there would hold the note for ever. (Raised
    with the firm: should the register supply the carrying amount by class,
    or do those rows belong in the Total column alone?)

    Returns None when the note has no classes yet, so the caller can hold
    the table and say what it needs.
    """
    from . import document_fields

    table = library_table(spec.get("version_id"), spec.get("table_id")) or {}
    library_rows = table.get("rows") or []
    scope = table.get("table_id") or ""
    classes = document_fields.classes(financial_year, scope)
    if not library_rows or not classes:
        return None

    figures = figures_for(financial_year)
    # The Total column is always presented and is never one of the
    # declared classes, however a preparer happened to type the list.
    columns = [name for name in classes
               if name.strip().lower() != TOTAL_COLUMN.lower()]
    columns.append(TOTAL_COLUMN)

    rows = []
    for library_row in library_rows:
        binding = (library_row.get("binding") or "").strip()
        row = {"label": library_row.get("label"), "cells": [],
               "bold": False, "rule": False}

        if binding == "STATIC" or not binding:
            row["cells"] = [None] * len(columns)
            row["bold"] = True
            rows.append(row)
            continue

        token = binding.split(":", 1)[0]
        field = binding.split(":", 1)[-1]
        # In a by-class table every document field is stated per class;
        # a row bound to a line code is not, because the trial balance
        # carries one figure for the whole line.
        per_class = token in document_fields.SCOPED

        for column in columns:
            if per_class:
                entered = document_fields.value(
                    financial_year, token, field, scope, member=column)
                if entered is not None:
                    row["cells"].append(entered.amount
                                        if entered.amount is not None else ZERO)
                else:
                    row["cells"].append(Held(
                        f"{DOCUMENT_TOKENS.get(token, 'A document')} has not "
                        f"given {field} for {column}",
                        blocking=document_fields.is_blocking(
                            financial_year, token, field, scope),
                        token=token, field=field, scope=scope, member=column))
            elif column == TOTAL_COLUMN:
                row["cells"].append(figures.resolve(binding, 0, scope))
            else:
                # The library states no per-class figure for this row.
                row["cells"].append(None)

        # The carrying amount rows: the ones bound to a line code rather
        # than to a document. They close each year's movement, so they
        # carry the rule above them the way a total does. Decided by what
        # the row is bound to, not by naming the codes - this library has
        # three such notes and a later one may have four.
        if not per_class and binding != "STATIC":
            row["bold"] = row["rule"] = True
        rows.append(row)

    shown = [row for row in rows
             if not _all_nil(row) and not _all_optional(row)]
    if not shown:
        return None

    for row in shown:
        for index, value in enumerate(row["cells"]):
            if _is_held(value):
                row.setdefault("held", {})[index] = value.reason
                row.setdefault("held_edit", {})[index] = (
                    {"token": value.token, "field": value.field,
                     "scope": value.scope, "member": value.member}
                    if value.editable else None)
                row["cells"][index] = None

    return {"heading": spec.get("heading"), "table_id": table.get("table_id"),
            "by_class": True, "classes": columns, "rows": shown,
            "columns": table.get("column_labels")}


def _all_nil(row):
    """A movement line with nothing in any column - suppressed, as ever."""
    values = [v for v in row["cells"] if v is not None]
    if not values or any(_is_held(v) for v in values):
        return False
    return not any(values)


def _all_optional(row):
    """Every column waiting on a figure the library does not require."""
    held = [v for v in row["cells"] if _is_held(v)]
    if not held or any(isinstance(v, Decimal) for v in row["cells"]):
        return False
    return all(not v.blocking for v in held)


def shareholdings(financial_year):
    """[(director, shares at the start, shares at the end)] on file."""
    from ..models import DocumentFigure

    rows = {}
    for figure in DocumentFigure.query.filter_by(
            financial_year_id=financial_year.id, token="REG").all():
        if figure.field in ("shares_open", "shares_close") and figure.member:
            rows.setdefault(figure.member, {})[figure.field] = figure.amount
    return [(name, values.get("shares_open"), values.get("shares_close"))
            for name, values in sorted(rows.items())]


def _shareholding_table(spec, financial_year):
    """The directors' shareholdings: name, shares at the start, at the end.

    Two columns of share COUNTS at two dates, not this year and last, so it
    cannot go through the ordinary table builder - which is why it printed
    "several columns per year, which is not laid out yet". The figures are
    the share register's, which is where the library says they come from;
    where nobody has supplied them the table says so.
    """
    rows = shareholdings(financial_year)
    if not rows:
        return {"heading": spec.get("heading"), "rows": [],
                "held_table": ["Needs the company's registers (the directors' "
                               "holdings at both dates)"],
                "table_id": spec.get("table_id")}
    return {"heading": spec.get("heading"), "shareholdings": True,
            "rows": [{"label": name, "open": opened, "close": closed}
                     for name, opened, closed in rows],
            "table_id": spec.get("table_id")}


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

    # Presented one column per class of asset - the fixed asset,
    # investment property and intangibles movement tables.
    if str(table.get("column_labels") or "").strip().lower().startswith(
            BY_CLASS_COLUMNS):
        laid_out = by_class_table(spec, financial_year)
        if laid_out is not None:
            return laid_out

    if "REG:shares_open" in str(table.get("line_codes") or ""):
        return _shareholding_table(spec, financial_year)

    # Number of shares and amount, for each year: four figure columns.
    from . import share_capital
    if share_capital.is_share_capital_table(table):
        built = share_capital.table(spec, financial_year, table,
                                    figures_for(financial_year))
        if built is not None:
            return built

    figures = figures_for(financial_year)
    first_year = bool(financial_year.is_first_year)
    # Which table these rows belong to. A document field whose name
    # repeats across notes is answered per note, so the row has to say
    # which note it is in before it can be resolved.
    scope = table.get("table_id") or ""
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
                _fill(row, code, figures, first_year, scope)
                rows.append(row)
            continue

        # One row per trial balance account on the line, under the account's
        # own caption. The library used to bind this to the whole line, which
        # printed the line total once as a row and again as the total.
        if binding.startswith("PERACCOUNT:"):
            code = binding[len("PERACCOUNT:"):].strip()
            last_year = figures.named_totals(1) if not first_year else None
            on_line = figures.accounts_on(code)
            for account_id, name, amount in on_line:
                row = _row(name or figures.label(code), binding)
                row["current"] = amount
                if first_year:
                    row["previous"] = None
                elif last_year is None:
                    # Known only in total. With ONE account on the line this
                    # year, the line's total is that row's - "Sales" - and
                    # nothing else can be split out of it.
                    whole = (figures.resolve(code, 1)
                             if len(on_line) == 1 else None)
                    row["previous"] = (
                        whole if isinstance(whole, Decimal) else Held(
                            "Last year is known only in total, so it cannot "
                            "be split by account"))
                else:
                    row["previous"] = last_year.get(
                        " ".join(name.split()).lower(), ZERO)
                row["ids"] = [account_id]
                rows.append(row)
            if not any(r["binding"] == binding for r in rows):
                row = _row(label, binding)
                _fill(row, code, figures, first_year, scope)
                rows.append(row)
            continue

        row = _row(label, binding)
        if binding.startswith("BALANCE:"):
            code = binding[len("BALANCE:"):].strip()
            for column, offset in (("current", 0), ("previous", 1)):
                if column == "previous" and first_year:
                    row[column] = None
                    continue
                whole = figures.section_total(code, offset)
                above = [r[column] for r in rows
                         if r["binding"] not in ("STATIC", "DOC:total")]
                if _is_held(whole):
                    row[column] = whole
                elif any(_is_held(v) or v is None for v in above):
                    row[column] = Held("The rows above it are not all known")
                else:
                    row[column] = whole - sum(above, ZERO)
            rows.append(row)
            continue
        if binding == "DOC:total":
            row["bold"] = row["rule"] = True
        elif not single_column and binding != "STATIC":
            row["current"] = Held(
                "Nobody has listed the classes of asset this note is "
                "presented in"
                if str(table.get("column_labels") or "").strip().lower()
                .startswith(BY_CLASS_COLUMNS)
                else "This table has several columns per year, which is "
                     "not laid out yet")
            row["previous"] = None if first_year else row["current"]
        elif counts.get(binding, 0) > 1:
            row["current"] = Held("The library binds more than one row of this "
                                  "table to the same figure")
            row["previous"] = None if first_year else row["current"]
        else:
            _fill(row, binding, figures, first_year, scope)
        rows.append(row)

    # A figure typed into an Incomplete cell counts from here on, so the
    # totals below add it in.
    table_key = table.get("table_id") or ""
    _make_answerable(rows, table_key, financial_year, totals=False)

    totals = [i for i, row in enumerate(rows) if row["binding"] == "DOC:total"]
    for index in totals:
        _doc_total(rows, index, figures, first_year,
                   named=table.get("totals_agree_with")
                   if index == totals[-1] else None,
                   partway=index != totals[-1])
    _make_answerable(rows, table_key, financial_year, totals=True)

    shown = [row for row in rows if not _nil(row) and not _optional_gap(row)]

    # A table made entirely of document fields and preparer answers used to be
    # shown as one sentence, which nobody could type into. It is shown as the
    # grid, each Incomplete cell answerable - unless the note is not needed at
    # all (no balance behind it), which _held_table still decides.
    if not any(_is_figure(row["binding"])
               and any(isinstance(v, Decimal) and v for v in
                       (row["current"], row["previous"]))
               for row in shown):
        if _held_table(spec, table, rows, figures) is None:
            return None

    for row in shown:
        if not row.get("ids"):
            row["ids"] = (figures.account_ids(row["binding"])
                          if _is_figure(row["binding"]) else [])
        if len(row["ids"]) == 1:
            row["ref"] = f"tb:{row['ids'][0]}"
        for column in ("current", "previous"):
            if _is_held(row[column]):
                held = row[column]
                row[f"held_{column}"] = held.reason
                row[f"held_{column}_edit"] = (
                    {"token": held.token, "field": held.field,
                     "scope": held.scope, "member": held.member}
                    if held.editable else None)
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


def _slug(label):
    text = "".join(ch if ch.isalnum() else "_" for ch in (label or "").lower())
    return "_".join(part for part in text.split("_") if part)[:80] or "row"


def _make_answerable(rows, table_key, financial_year, totals=False):
    """Give every Incomplete cell a place to type its answer.

    Only a hold naming an exact document figure (the tax computation, the aged
    listing) could be answered on the spot; everything else - a preparer's
    entry, last year's split, a total nothing states - printed the plain word
    Incomplete with nothing to click. Those now carry an ENTERED answer keyed
    by the table, the row and the year, saved through the same function the
    Figures page uses. A typed figure replaces the hold, so the totals add it
    in and the note stops being incomplete.
    """
    from . import document_fields

    seen = {}
    for row in rows:
        binding = row.get("binding")
        if binding == "STATIC" or (binding == "DOC:total") != totals:
            continue
        base = _slug(row.get("label"))
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            base = f"{base}_{seen[base]}"
        for column, suffix in (("current", ""), ("previous", "__prior")):
            value = row.get(column)
            if not _is_held(value) or value.editable:
                continue
            field = base + suffix
            entered = document_fields.value(financial_year, "ENTERED", field,
                                            table_key)
            if entered is not None:
                row[column] = (entered.amount if entered.amount is not None
                               else ZERO)
            else:
                row[column] = Held(value.reason, whole_year=value.whole_year,
                                   blocking=value.blocking, token="ENTERED",
                                   field=field, scope=table_key)


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


def _fill(row, binding, figures, first_year, scope=""):
    row["current"] = figures.resolve(binding, 0, scope)
    row["previous"] = (None if first_year
                       else figures.resolve(binding, 1, scope))


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


def _doc_total(rows, index, figures, first_year, named=None, partway=False):
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

    # The library ties this total to no statement line ("no total"): it is the
    # table's own rows added together, so that is what it prints - where every
    # row above is a figure. Asked of a statement line instead, it was held
    # because the line (reserves) also carries share capital, which this table
    # does not list.
    if str(named or "").strip().lower() == "no total":
        for column in ("current", "previous"):
            if column == "previous" and first_year:
                continue
            values = [r[column] for r in group]
            if values and all(isinstance(v, Decimal) for v in values):
                row[column] = sum(values, ZERO)
            elif any(_is_held(v) for v in values):
                row[column] = Held("The rows above it are not all known")
            else:
                row[column] = None
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

    # A subtotal partway down a table ("Trade receivables - net", the rows
    # above it less the loss allowance) is on no statement, so nothing states
    # it and it was held Incomplete in both years. It is the rows above it,
    # and where every one of them is a figure that is what it says.
    if partway:
        for column in ("current", "previous"):
            if column == "previous" and first_year:
                continue
            if not _is_held(row[column]):
                continue
            values = [r[column] for r in group]
            if values and all(isinstance(v, Decimal) for v in values):
                row[column] = sum(values, ZERO)


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
