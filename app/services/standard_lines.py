"""The client's standard statement lines, and the rules that place an account on one.

The client keeps a workbook - "AuditMate Standard Statement Lines" - that says,
for every line of the statements, what belongs on it: the account class it
accepts, the words in an account's name that put it there, the wording last
year's sets used, and the notes-library line code it carries. When they revise
it (this is version 8) the app must take the revision as it stands, not have
someone re-key it. This reads the workbook into config/standard_lines.yaml and
places accounts by the rules the workbook's Read me states:

  1. Class first. Only lines whose account class is the account's are
     considered: a bank charges expense can never land in Cash, the GST control
     account (a liability) can never land in the profit and loss.
  2. A keyword matches at the start of a word in the account's name, whatever
     the case: "rent" matches "Rent expense" and not "Current account".
  3. The longest matching keyword wins: "staff house" beats "rent".
  4. Two different lines tied on the longest keyword: the accountant decides.
  5. No match: a profit and loss expense goes to Other expenses, flagged for
     review; anything else goes to the accountant.
  6. Never placed automatically, whatever else matches: the whole words
     contra, suspense, clearing, rounding, and the phrases repayment of loan,
     opening balance, historical adjustment, unallocated.
  7. The accountant confirms once; it is remembered for the client (the app's
     existing customer rules, which are consulted before this).
"""
import functools
import logging
import re
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

STATEMENTS = {"Profit and Loss": "profit_and_loss",
              "Balance Sheet": "balance_sheet",
              "Changes in Equity": "changes_in_equity",
              "Cash Flow": "cash_flow",
              "Ageing": "ageing"}
CLASSES = {"P&L income": "pl_income", "P&L expense": "pl_expense",
           "BS asset": "bs_asset", "BS liability": "bs_liability",
           "BS equity": "bs_equity"}

# Rule 6, as the Read me states it. Whole words / phrases, any case.
NEVER_AUTO = ("contra", "suspense", "clearing", "rounding", "repayment of loan",
              "opening balance", "historical adjustment", "unallocated")

OTHER_EXPENSES_KEY = "other_expenses"


# ------------------------------------------------------------------ reading

def _cells(row):
    return [("" if v is None else str(v).strip()) for v in row]


def _split_keywords(text):
    return [w.strip().lower() for w in re.split(r"[,;]", text or "") if w.strip()]


def build_from_workbook(path):
    """Read the workbook into {"version", "lines": [...], "never_auto": [...]}."""
    import openpyxl

    wb = openpyxl.load_workbook(str(path), data_only=True)

    # --- what the workbook says about itself
    version = ""
    if "Read me" in wb.sheetnames:
        first = next(iter(wb["Read me"].iter_rows(values_only=True)), None)
        version = str(first[0]) if first and first[0] else ""

    # --- the Technical reference: internal key and library codes per line
    tech = {}
    if "Technical reference" in wb.sheetnames:
        rows = list(wb["Technical reference"].iter_rows(values_only=True))
        head = next((i for i, r in enumerate(rows) if r and r[0] == "Statement"), None)
        if head is not None:
            names = _cells(rows[head])
            for raw in rows[head + 1:]:
                cells = dict(zip(names, _cells(raw)))
                if not cells.get("Statement"):
                    continue
                key = (cells["Statement"], cells.get("Section", ""), cells.get("Line", ""),
                       cells.get("Level", ""))
                tech[key] = {
                    "key": cells.get("Internal key") or None,
                    "default_code": cells.get("Library line code (default)") or None,
                    "codes": [c.strip() for c in
                              (cells.get("Notes library line code(s)") or "").split(",")
                              if c.strip()],
                    "status": cells.get("Status") or None,
                    "note": cells.get("Development note") or None,
                    "nature_group": cells.get("Nature group") or None,
                    "rolls_into": cells.get("Rolls into") or None,
                }

    lines = []
    for sheet_name, statement in STATEMENTS.items():
        if sheet_name not in wb.sheetnames:
            continue
        rows = list(wb[sheet_name].iter_rows(values_only=True))
        head = next((i for i, r in enumerate(rows)
                     if r and r[0] == "Section" and r[1]), None)
        if head is None:
            continue                          # a sheet without the line table
        names = _cells(rows[head])
        section = ""
        for raw in rows[head + 1:]:
            cells = dict(zip(names, _cells(raw)))
            if cells.get("Section"):
                section = cells["Section"]
            caption = cells.get("Line in the statements") or cells.get("Line") or ""
            level = cells.get("Level") or ""
            if not caption or not level:
                continue
            reference = tech.get((sheet_name, section, caption, level), {})
            lines.append({
                "statement": statement,
                "section": section,
                "caption": caption,
                "level": level,
                "rolls_into": cells.get("Rolls into") or reference.get("rolls_into"),
                "nature_group": cells.get("Nature group") or reference.get("nature_group"),
                "source": cells.get("Source") or None,
                "account_class": CLASSES.get(cells.get("Account class", "")),
                "keywords": _split_keywords(cells.get("Example account names")),
                "prior_captions": [c.strip() for c in
                                   (cells.get("Prior-year captions matched") or "").split(";")
                                   if c.strip()],
                "key": reference.get("key"),
                "default_code": reference.get("default_code"),
                "codes": reference.get("codes") or [],
                "status": reference.get("status"),
            })

    return {"version": version, "lines": lines, "never_auto": list(NEVER_AUTO)}


def dump(data, path):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("# Read from the client's AuditMate Standard Statement Lines "
                     "workbook by `flask import-standard-lines`.\n"
                     "# Do not edit by hand: import the next version instead.\n")
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True,
                       width=100, default_flow_style=False)


@functools.lru_cache(maxsize=1)
def _load(path_str):
    path = Path(path_str)
    if not path.exists():
        return {"lines": [], "never_auto": list(NEVER_AUTO)}
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {"lines": [], "never_auto": list(NEVER_AUTO)}


def load():
    from flask import current_app

    return _load(str(Path(current_app.config["CONFIG_DIR"]) / "standard_lines.yaml"))


# ---------------------------------------------------------------- matching

def account_class(account_type, statement_type=None):
    """pl_income / pl_expense / bs_asset / bs_liability / bs_equity, from the
    type the client's books give the account - or None when it does not say."""
    from .mapping import side_for_account_type, statement_for_account_type

    text = (account_type or "").strip().lower()
    if not text:
        return None
    statement = statement_for_account_type(account_type)
    if statement == "balance_sheet":
        side = side_for_account_type(account_type)
        return {"asset": "bs_asset", "liability": "bs_liability",
                "equity": "bs_equity"}.get(side)
    if statement == "profit_and_loss":
        income = ("revenue", "income", "sales", "other income")
        if any(w in text for w in income) and "expense" not in text:
            return "pl_income"
        return "pl_expense"
    return None


def _word_start(keyword):
    return re.compile(r"(?<![a-z0-9])" + re.escape(keyword), re.IGNORECASE)


@functools.lru_cache(maxsize=1)
def _compiled(path_str):
    data = _load(path_str)
    compiled = []
    for line in data.get("lines") or []:
        if not (line.get("keywords") and line.get("key") and line.get("account_class")):
            continue
        compiled.append((line, [(k, _word_start(k)) for k in line["keywords"]]))
    never = [re.compile(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", re.IGNORECASE)
             for p in data.get("never_auto") or NEVER_AUTO]
    return compiled, never


def _compiled_now():
    from flask import current_app

    return _compiled(str(Path(current_app.config["CONFIG_DIR"]) / "standard_lines.yaml"))


def never_auto(label):
    _, never = _compiled_now()
    return any(p.search(label or "") for p in never)


def place(label, klass):
    """(result, detail) for one account name of one class.

    result: 'placed'    detail = (line, keyword)
            'tie'       detail = [(line, keyword), ...] on the longest keyword
            'never'     the name is on the never-place list
            'none'      nothing matched
    """
    compiled, never = _compiled_now()
    text = (label or "").strip()
    if any(p.search(text) for p in never):
        return "never", None
    best_len, hits = 0, []
    for line, keywords in compiled:
        if klass is not None and line["account_class"] != klass:
            continue
        for keyword, pattern in keywords:
            if pattern.search(text):
                if len(keyword) > best_len:
                    best_len, hits = len(keyword), [(line, keyword)]
                elif len(keyword) == best_len:
                    hits.append((line, keyword))
    if not hits:
        return "none", None
    lines = {h[0]["key"]: h for h in hits}
    if len(lines) > 1:
        return "tie", list(lines.values())
    return "placed", hits[0]


def statement_of(key):
    """Which statement a standard key is drawn on, from the app's own templates."""
    from .classify import classify

    entry = classify(key)
    if not entry:
        return None
    return {"P&L": "profit_and_loss", "Balance Sheet": "balance_sheet"}.get(entry["fs"])


# ------------------------------------------------------------------ overlays
#
# The workbook names, for every line, the notes-library line code it carries
# by default and the others it may carry. Those are merged into the two hand
# written files that already say the same thing for the app's own lines, never
# replacing them: a line the app already knows keeps its codes and default and
# gains any the workbook adds; a line it did not know is created.

# Version 8 splits "Other payables" from "Trade payables"; until the two are
# split in the stored data (a migration) both are the app's one payables line.
KEY_ALIASES = {"other_payables": "trade_payables"}


def _landing():
    for line in (load().get("lines") or []):
        if line.get("key") and line.get("keywords"):
            yield line


def app_key(key):
    return KEY_ALIASES.get(key, key)


def overlay_categories(categories):
    """Add the workbook's line codes to line_code_categories.yaml's lines."""
    from .classify import _index

    known = set(_index())
    for line in _landing():
        key = app_key(line["key"])
        if key not in known:
            continue
        wanted = [c for c in ([line.get("default_code")] + list(line.get("codes") or []))
                  if c]
        if not wanted:
            continue
        spec = categories.get(key)
        if spec is None:
            spec = {"codes": []}
            categories[key] = spec
        spec.setdefault("codes", [])
        for code in wanted:
            if code not in spec["codes"]:
                spec["codes"].append(code)
        if not spec.get("default") and len(spec["codes"]) > 1 and line.get("default_code"):
            spec["default"] = line["default_code"]
    return categories


def overlay_code_map(codes):
    """Let each library code the workbook gives a line reach that line."""
    from .classify import _index

    known = set(_index())
    for line in _landing():
        key = app_key(line["key"])
        if key not in known:
            continue
        for code in ([line.get("default_code")] + list(line.get("codes") or [])):
            if not code:
                continue
            current = codes.setdefault(code, [])
            if current is None:
                current = codes[code] = []
            if key not in current:
                current.append(key)
    return codes
