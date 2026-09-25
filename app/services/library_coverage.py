"""What the app actually reads of a notes library - and what it would only show
as missing.

A library workbook is a specification. The app holds all of it, but holding a
sheet is not reading it: a binding token nothing implements, a line code no
account can be placed on, a document field the Figures page has no question
for, all print "Incomplete" for ever, and from the report alone that looks like
missing data rather than a library that asks for something the app cannot yet
do. This says which is which, every time a library is imported and for the
libraries already in the database.

Three questions, each answered from the code itself, not from a list kept beside
it:

  * SHEETS  - which of the workbook's sheets does any engine module read?
  * TOKENS  - for every binding token the tables and paragraphs use, can the
              engine resolve it, does it wait on a document (and is that
              document's field defined, so the preparer can be asked for it),
              or is there nothing in the engine that reads it?
  * LINES   - for every statement line code the library defines, is there a
              route by which an account or a computation reaches it?
"""
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Sheets read when the library is IMPORTED (to build the notes themselves) are
# not "read by the engine": they are the workbook's own body.
_IMPORT_ONLY = {"Notes", "Paragraphs", "Tables", "Version"}

_SHEET_CALL = re.compile(
    r"""(?:\.sheet\(\s*|_sheet\(\s*[^,()]+,\s*|reference\.get\(\s*|reference\[\s*)
        ["']([^"']+)["']""", re.VERBOSE)

_TOKEN_SHAPE = re.compile(r"^(?:[A-Z][A-Za-z]*:\S*|(?:BS|PL|CF|EQ)-[A-Z0-9-]+)$")

_cache = {}


def sheet_readers():
    """{sheet name: [module, ...]} for every sheet some engine code reads."""
    if "sheets" in _cache:
        return _cache["sheets"]
    root = Path(__file__).resolve().parent.parent
    readers = {}
    for path in list(root.glob("services/*.py")) + list(root.glob("blueprints/*.py")):
        if path.name in ("note_library.py", "library_coverage.py"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for name in _SHEET_CALL.findall(text):
            readers.setdefault(name, set()).add(path.stem)
    _cache["sheets"] = {k: sorted(v) for k, v in readers.items()}
    return _cache["sheets"]


# ---------------------------------------------------------------- tokens ----

def _engine_tokens():
    from . import bindings

    return (dict(bindings.DOCUMENT_TOKENS), set(bindings.PERSON_TOKENS))


# Forms the engine's resolver dispatches on before it treats a token as a line
# code. Kept in step with Figures.resolve; the coverage test resolves a sample
# of each so the two cannot drift silently.
_WRAPPERS = ("PRIOR:", "SUM:", "EACH:", "PERACCOUNT:", "PERCLASS:", "BALANCE:",
             "FI:")
_LINE_PREFIXES = ("BS-", "PL-", "CF-", "EQ-")
_LITERALS = {"STATIC", "DOC:total"}


def classify_token(token, line_codes, fields):
    """(status, reason) for one binding token.

    status: 'reads'    - the engine resolves it from the books
            'document' - it waits on a document or a person, and the library
                         defines the field so the preparer can be asked for it
            'no_field' - it waits on a document field the library does not
                         define: nothing can ever answer it
            'no_reader'- nothing in the engine implements this kind of token
    """
    documents, persons = _engine_tokens()
    token = (token or "").strip()
    if not token or token in _LITERALS:
        return "reads", "a label or a total the source states"
    if token in persons:
        return "document", "entered by the preparer or read from a record"
    if token.startswith(_LINE_PREFIXES):
        return ("reads", "a statement line") if token in line_codes else (
            "no_field", f"line code {token} is not on the Statement lines sheet")
    for prefix in _WRAPPERS:
        if token.startswith(prefix):
            inner = token[len(prefix):]
            if prefix == "SUM:" or prefix == "EACH:":
                bad = [c for c in inner.split("+") if c and c not in line_codes]
                if bad:
                    return "no_field", "line code(s) not defined: " + ", ".join(bad)
                return "reads", "lines added together"
            if prefix == "FI:":
                return "reads", "the financial-instrument lines"
            return classify_token(inner, line_codes, fields)
    prefix, _, field = token.partition(":")
    if prefix in documents:
        if (prefix, field) in fields or not field:
            return "document", documents[prefix]
        return "no_field", (f"{prefix}:{field} is not on the Binding fields sheet, "
                            f"so the preparer is never asked for it")
    return "no_reader", f"the engine has no reader for {prefix or token}:"


def token_report(pieces, line_codes, fields):
    """{status: {(token, reason): [where, ...]}} over every binding in the library."""
    check = lambda token: classify_token(token, line_codes, fields)      # noqa: E731

    out = {"reads": {}, "document": {}, "no_field": {}, "no_reader": {}}
    for piece in pieces:
        where = piece.get("table_id") or piece.get("para_id") or piece.get("ref")
        tokens = list(piece.get("row_bindings") or [])
        # A paragraph's "Binds to" is usually a sentence about where a figure
        # comes from ("Cash accounts"). Only a value shaped like a token is one.
        binds = (piece.get("binds_to") or "").strip()
        if _TOKEN_SHAPE.match(binds):
            tokens.append(binds)
        for token in tokens:
            token = (token or "").strip()
            if not token:
                continue
            status, reason = check(token)
            out[status].setdefault((token, reason), []).append(where)
    return out


# ----------------------------------------------------------------- lines ----

def line_routes():
    """Line codes the app can land something on: from the account mapping
    (which library code each standard line and each finer category carries) and
    from the statement definitions themselves."""
    if "routes" in _cache:
        return _cache["routes"]
    import yaml
    from flask import current_app

    config = Path(current_app.config["CONFIG_DIR"])
    routes = set()
    for name in ("line_code_map.yaml", "line_code_categories.yaml"):
        path = config / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        routes |= set(re.findall(r"\b(?:BS|PL|CF|EQ)-[A-Z0-9][A-Z0-9-]*", text))
    templates = config / "statement_templates.yaml"
    if templates.exists():
        routes |= set(re.findall(r"\b(?:BS|PL|CF|EQ)-[A-Z0-9][A-Z0-9-]*",
                                 templates.read_text(encoding="utf-8")))
    _cache["routes"] = routes
    return routes


def unreachable_lines(line_rows):
    """Library line codes nothing in the app can land a figure on."""
    routes = line_routes()
    out = []
    for row in line_rows:
        code = row.get("Line code")
        if not code or code in routes:
            continue
        out.append((row.get("Statement"), code, row.get("Line label")))
    return sorted(out, key=lambda r: (str(r[0]), r[1]))


# ---------------------------------------------------------------- report ----

def _pieces_from_data(data):
    from .note_library import _pieces_for

    out = []
    for note in data["notes"]:
        out.extend(_pieces_for(note))
    return out


def _pieces_from_version(version):
    out = []
    for note in version.notes:
        out.extend(note.pieces or [])
    return out


def build(reference, pieces):
    """The report as data: a dict a person or a page can print."""
    line_rows = reference.get("Statement lines") or []
    line_codes = {r.get("Line code") for r in line_rows if r.get("Line code")}
    fields = {(r.get("Token"), r.get("Field"))
              for r in reference.get("Binding fields") or []
              if r.get("Token") and r.get("Field")}
    readers = sheet_readers()
    sheets = []
    for name, rows in sorted(reference.items()):
        if name in _IMPORT_ONLY:
            continue
        sheets.append({"sheet": name, "rows": len(rows or []),
                       "read_by": readers.get(name, [])})
    return {"sheets": sheets,
            "tokens": token_report(pieces, line_codes, fields),
            "unreachable": unreachable_lines(line_rows),
            "line_codes": len(line_codes)}


def for_data(data):
    return build(data.get("reference") or {}, _pieces_from_data(data))


def for_version(version):
    reference = {s.name: (s.rows or []) for s in version.sheets}
    return build(reference, _pieces_from_version(version))


def render(report, echo, full=False):
    """Print the report through `echo` (click.echo)."""
    unread = [s for s in report["sheets"] if not s["read_by"]]
    read = [s for s in report["sheets"] if s["read_by"]]
    echo("")
    echo("  ENGINE COVERAGE - what the app reads of this library")
    echo(f"    Sheets read by the engine: {len(read)} of {len(report['sheets'])}"
         " reference sheets")
    if unread:
        echo("    Held but read by no engine code (reference only):")
        for s in unread:
            echo(f"      - {s['sheet']}  ({s['rows']} rows)")
    if full:
        for s in read:
            echo(f"      read  {s['sheet']:<26} by {', '.join(s['read_by'])}")

    tokens = report["tokens"]
    counts = {k: len(v) for k, v in tokens.items()}
    echo(f"    Binding tokens: {counts['reads']} read from the books, "
         f"{counts['document']} wait on a document or a person, "
         f"{counts['no_field']} can never be answered, "
         f"{counts['no_reader']} have no reader in the engine")
    for status, title in (("no_reader", "No reader in the engine - these print Incomplete for ever"),
                          ("no_field", "Defined nowhere in the library - nothing can answer them")):
        rows = tokens[status]
        if not rows:
            continue
        echo(f"    {title}:")
        for (token, reason), where in sorted(rows.items()):
            shown = ", ".join(sorted({str(w) for w in where})[:3])
            echo(f"      ! {token}  - {reason}  (used by {shown}"
                 f"{' ...' if len(set(map(str, where))) > 3 else ''})")

    unreachable = report["unreachable"]
    echo(f"    Statement line codes: {report['line_codes']} in the library, "
         f"{len(unreachable)} with no route from any account or statement line")
    for statement, code, label in unreachable[:60]:
        echo(f"      ! {statement or '-':<6} {code:<16} {label or ''}")
    if len(unreachable) > 60:
        echo(f"      ... and {len(unreachable) - 60} more")
