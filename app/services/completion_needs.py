"""What a report still needs before it can be issued - as files and answers.

Each note carries the reasons it is incomplete, in the words of the engine
("Needs the tax computation (current)"). That is right for the note and no
help to a person deciding what to chase: fifty reasons across a dozen notes
are five or six things to ask for. This groups them by what would settle them,
and says who has it and where it goes in.

The grouping is done on the reasons the engine actually wrote, so it can never
list a file the report does not need, or miss one it does.
"""
import re

# (key, pattern on the reason text). First match wins, so the specific ones
# come first.
_RULES = [
    ("stale", re.compile(r"^update last year's figure", re.I)),
    ("reconcile", re.compile(r"cannot explain|do not add up|difference", re.I)),
    ("practitioner", re.compile(r"practitioner", re.I)),
    ("prior_docs", re.compile(
        r"no earlier engagement|year before the comparative", re.I)),
    ("loan", re.compile(r"loan and lease schedule|loan figures|amortisation",
                        re.I)),
    ("ledger", re.compile(r"general ledger", re.I)),
    ("tax", re.compile(r"tax computation|iras", re.I)),
    ("aged", re.compile(r"aged receivable", re.I)),
    ("register", re.compile(r"share register|company's registers|registers", re.I)),
    ("far", re.compile(r"fixed asset register|right-of-use|rou_|lease agreement",
                       re.I)),
    ("questions", re.compile(r"^waiting for the preparer", re.I)),
    ("related", re.compile(r"related part", re.I)),
    ("client_record", re.compile(
        r"client record|not provided|business profile|client master", re.I)),
    ("prior_split", re.compile(
        r"last year.{0,80}(not split|one figure|known only in total|"
        r"signed accounts|signed set)|signed accounts.{0,60}not split|"
        r"known only in total|rows above it|less the rows above", re.I)),
    ("layout", re.compile(
        r"several columns per year|not laid out|not built from the trial "
        r"balance yet|does not belong to a statement line", re.I)),
    ("preparer_entry", re.compile(
        r"to be entered by the preparer|no document states this total|"
        r"preparer supplies|for the preparer to confirm", re.I)),
]

# What each group means, in the order a preparer would work through them.
_NEEDS = {
    "stale": {
        "label": "Last year's amounts in the wording",
        "file": "No file - sentences carried over from last year quote last "
                "year's amounts; each shows as [update: ...] in the note",
        "owner": "The preparer",
        "where": "Type this year's amount over the [update: ...] text, or "
                 "delete the sentence.",
        "endpoint": None,
        "kind": "answer",
    },
    "reconcile": {
        "label": "Balances that do not agree",
        "file": "No file - the books disagree with each other or with last "
                "year's signed accounts, and the difference has to be found",
        "owner": "The preparer",
        "where": "Statement of cash flows and statement of changes in equity.",
        "endpoint": None,
        "kind": "answer",
    },
    "tax": {
        "label": "Tax computation",
        "file": "The tax computation for the year, or the IRAS Notice of "
                "Assessment",
        "owner": "The client's tax agent",
        "where": "Upload it under Documents as a Tax Document, or type the "
                 "figures on the Figures page.",
        "endpoint": "documents.figures",
        "kind": "file",
    },
    "aged": {
        "label": "Aged receivables",
        "file": "The aged receivables listing as at the year end (in Xero: "
                "Aged Receivables Summary, dated the last day of the year)",
        "owner": "The client",
        "where": "Upload it under Documents as an Accounts Receivable "
                 "Listing, or type the buckets on the Figures page.",
        "endpoint": "documents.figures",
        "kind": "file",
    },
    "register": {
        "label": "Share register",
        "file": "The share register, or the ACRA BizFile extract",
        "owner": "The client",
        "where": "Type the share counts on the Figures page.",
        "endpoint": "documents.figures",
        "kind": "file",
    },
    "practitioner": {
        "label": "Practitioner name and address",
        "file": "The compiling firm's name and address (SSRS 4410)",
        "owner": "The firm",
        "where": "Type them once in Settings; they are used on every "
                 "customer's compilation report.",
        "endpoint": "settings.disclosures",
        "kind": "answer",
    },
    "loan": {
        "label": "Loan and lease schedules",
        "file": "Loan and lease agreements or the bank's amortisation "
                "schedule (repayments due within 1 year, 1-5 years, over 5 "
                "years; lease interest)",
        "owner": "The client",
        "where": "Type the figures on the Figures page.",
        "endpoint": "documents.figures",
        "kind": "file",
    },
    "ledger": {
        "label": "General ledger detail",
        "file": "The general ledger for the accounts named (related party "
                "allowance charge)",
        "owner": "The client",
        "where": "Upload it under Documents as a General Ledger.",
        "endpoint": None,
        "kind": "file",
    },
    "prior_docs": {
        "label": "Last year's comparative figures",
        "file": "The same documents for LAST year (tax computation, aged "
                "receivables, loan schedule) - a comparative column is only "
                "filled from last year's own document",
        "owner": "The client",
        "where": "Enter them against last year's engagement, or type them "
                 "into the Incomplete cells.",
        "endpoint": None,
        "kind": "file",
    },
    "far": {
        "label": "Fixed asset register / leases",
        "file": "The fixed asset register, and any lease agreements",
        "owner": "The client",
        "where": "Upload the register under Documents, or type the figure "
                 "on the Figures page (0 if there are none).",
        "endpoint": "documents.figures",
        "kind": "file",
    },
    "client_record": {
        "label": "Company details",
        "file": "Registration number, directors, company secretary and "
                "registered office",
        "owner": "The client (or the signed accounts you uploaded as the "
                 "template)",
        "where": "Type them on the customer page, or straight into the cover "
                 "page of the report.",
        "endpoint": "customers.detail",
        "kind": "answer",
    },
    "related": {
        "label": "Related party decisions",
        "file": "Nothing to upload - you decide who is a related party",
        "owner": "The preparer",
        "where": "Related parties page.",
        "endpoint": "reports.related_parties",
        "kind": "answer",
    },
    "questions": {
        "label": "Preparer questions",
        "file": "Nothing to upload - short answers about the company",
        "owner": "The preparer",
        "where": "Questions page.",
        "endpoint": "reports.preparer_inputs",
        "kind": "answer",
    },
    "prior_split": {
        "label": "Last year's split",
        "file": "Last year's figures in detail. The signed accounts give this "
                "line as one total, and last year's trial balance does not "
                "agree with the signed total, so no split is taken",
        "owner": "The client, or your last-year working papers",
        "where": "Type last year's figure into the Incomplete cell in the "
                 "report.",
        "endpoint": None,
        "kind": "answer",
    },
    "preparer_entry": {
        "label": "Figures only you can supply",
        "file": "No file holds these - covenants, contingencies, fair values "
                "and the like",
        "owner": "The preparer",
        "where": "Click the Incomplete cell in the report and type it.",
        "endpoint": None,
        "kind": "answer",
    },
    "layout": {
        "label": "Not built in the software yet",
        "file": "Nothing to supply - AuditMate does not build these lines or "
                "lay these tables out yet",
        "owner": "AuditMate",
        "where": "Tell the developer which note.",
        "endpoint": None,
        "kind": "software",
    },
    "other": {
        "label": "Other",
        "file": "See the reason under the note",
        "owner": "The preparer",
        "where": "Open the note in the report.",
        "endpoint": None,
        "kind": "answer",
    },
}

ORDER = ["stale", "reconcile", "tax", "aged", "loan", "register", "far", "ledger",
         "prior_docs", "practitioner", "client_record", "related",
         "questions", "prior_split", "preparer_entry", "layout", "other"]


def classify(reason):
    for key, pattern in _RULES:
        if pattern.search(reason or ""):
            return key
    return "other"


def needs(incomplete):
    """The report's open items, grouped by what would settle them.

    `incomplete` is what record_completeness returns: (title, reasons) pairs.
    [{key, label, file, owner, where, endpoint, kind, notes: [titles],
      count}], in working order. Empty when nothing is incomplete.
    """
    groups = {}
    for title, reasons in incomplete:
        for reason in reasons:
            key = classify(reason)
            group = groups.setdefault(key, {"reasons": set(), "notes": []})
            group["reasons"].add(reason)
            if title not in group["notes"]:
                group["notes"].append(title)

    out = []
    for key in ORDER:
        if key not in groups:
            continue
        item = dict(_NEEDS[key], key=key)
        item["notes"] = groups[key]["notes"]
        item["count"] = len(groups[key]["reasons"])
        out.append(item)
    return out


# How each group reads in the two-line marker on a note.
_SHORT = {
    "reconcile": "an explanation of the difference in the balances",
    "tax": "the tax computation",
    "aged": "the aged receivables listing",
    "loan": "the loan and lease schedules",
    "register": "the share register",
    "far": "the fixed asset register",
    "ledger": "the general ledger",
    "prior_docs": "last year's documents",
    "practitioner": "the practitioner's name and address (Settings)",
    "client_record": "the company details",
}
_ANSWER = {
    "stale": "last year's amounts in the wording updated",
    "related": "related-party decisions",
    "prior_split": "figures typed into the highlighted cells",
    "preparer_entry": "figures typed into the highlighted cells",
    "layout": "not built in the software yet",
}


def summarise(reasons):
    """A note's open items in at most two short lines.

    "Needs: the tax computation." and "You answer: 3 questions, figures typed
    into the highlighted cells." - not a list of every reason the engine
    wrote, which for a note with forty open cells ran to a wall of text.
    """
    files, answers, questions = [], [], 0
    for reason in reasons or []:
        key = classify(reason)
        if key == "questions":
            questions += 1
        elif key in _SHORT:
            if _SHORT[key] not in files:
                files.append(_SHORT[key])
        elif key in _ANSWER:
            if _ANSWER[key] not in answers:
                answers.append(_ANSWER[key])
        elif "other" not in answers:
            answers.append("the items shown in the note")
    if questions:
        answers.insert(0, f"{questions} question{'s' if questions != 1 else ''} "
                          f"on the Questions page")
    lines = []
    if files:
        lines.append("Needs: " + "; ".join(files) + ".")
    if answers:
        lines.append(("You answer: " if files or questions else "You supply: ")
                     + ", ".join(answers) + ".")
    return lines[:2]
