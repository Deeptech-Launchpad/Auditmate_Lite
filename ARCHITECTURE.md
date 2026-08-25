# What We Built — Auditmate Lite

A plain-language walkthrough of the application: what each part does, where AI
is used and why, and how a document becomes an audit report.

---

## 1. The big picture

The app is a **pipeline with a human checkpoint at every stage**. Nothing moves
forward on its own — an auditor signs off at each gate.

```
  UPLOAD          EXTRACT           REVIEW           MAP            GENERATE
  a document  →   read figures  →   auditor      →  labels to   →   financial
  (any format)    out of it         corrects        statement       statements
                                    them            lines              │
                                       ▲                                ▼
                                       │                            SHARE with
                                  the safety net                    customer
                                                                        │
                                                                        ▼
                                                                    APPROVED
                                                                        │
                                                                        ▼
                                                                  AUDIT REPORT
                                                                    (PDF)
```

The two rules that shape everything:

1. **Only verified data flows downstream.** A document that hasn't been signed
   off in Review & Correct is invisible to the statement builder. A misread
   number physically cannot reach a financial statement without a human
   passing it through.
2. **Every figure keeps a trail back to its source.** Each extracted row stores
   the sheet/cell or page it came from, and every correction writes an audit-log
   entry recording who changed what, from what, to what, and when.

---

## 2. Where AI is used — and where it deliberately isn't

You asked for AI wherever it improves accuracy. Here's exactly where it runs.

### AI is used in three places

**① Reading documents that rules can't parse** — `app/services/extraction/ai.py`

Scanned PDFs, photographed invoices, and files with unusual layouts have no
machine-readable structure. Claude reads the PDF or image **directly** (it
accepts PDFs natively and reads the page images), returning structured line
items. This replaces a traditional OCR step rather than sitting behind one,
which is more accurate — OCR gives you noisy text, Claude gives you the actual
table structure.

**② Grading its own confidence** — the same file

The model returns a `confidence` score *per row*, and is explicitly instructed
to score low when a figure is blurred or a column alignment was inferred. This
matters more than raw accuracy: a row flagged as uncertain gets a human check,
whereas a confidently-wrong number is the dangerous case. The system prompt says
so directly:

> *"Accuracy matters more than completeness here — a flagged row is cheap, a
> wrong number in a financial statement is not."*

**③ Mapping unfamiliar account names** — `app/services/mapping.py`

Every client names their accounts differently: "Sundry Debtors", "Trade
Debtors", "A/R — Trade" all mean *trade receivables*. Around 120 seed rules
cover the common ones. Anything left over goes to Claude, which maps it to the
correct SFRS statement line. Even here, a mapping is only accepted automatically
when the model's confidence is ≥ 0.7 — below that it goes to the auditor's
Unmapped tray rather than silently into the accounts.

### AI is deliberately NOT used for

| Task | Why not |
|---|---|
| Reading clean Excel / CSV | A number in cell B12 *is* that number. Deterministic parsing is exact, instant and free — inference could only make it worse. |
| Arithmetic (totals, subtotals, profit, balance checks) | All maths is plain Python in `compute.py`. LLMs should never be trusted with arithmetic when ordinary code is exact. |
| Deciding what's correct | AI proposes, the auditor disposes. Nothing AI produces is auto-accepted into a statement. |

### The routing logic (`app/services/extraction/__init__.py`)

Rules run first; AI is called only when rules can't do the job:

| Situation | What runs | Cost |
|---|---|---|
| Clean `.xlsx` / `.csv` | openpyxl / csv parser | free |
| `.docx` with real tables | python-docx | free |
| Typed PDF with detectable tables | pdfplumber | free |
| Rule parser returned low confidence | pdfplumber **then** Claude | one API call |
| Scanned PDF (no text layer) | Claude directly | one API call |
| Image upload | Claude directly | one API call |
| No API key configured | rules only; unreadable rows flagged | free |

So a firm uploading mostly spreadsheets pays almost nothing; the AI cost scales
with how messy the incoming documents actually are.

---

## 3. The scripts and what each one does

### Things you run

| File | What it is |
|---|---|
| `setup.ps1` | One-shot environment setup: installs Python + PostgreSQL, creates the venv and database, writes `.env`, creates tables, loads demo data. |
| `wsgi.py` | The app entry point. `flask run` locally; `gunicorn wsgi:app` in production. |
| `worker.py` | Optional background process for extraction jobs. Only needed when `JOBS_INLINE=0`. For the demo, extraction runs inline so you don't need this. |
| `app/cli.py` | The `flask` commands: `init-db`, `seed-demo`, `create-admin`, `reset-db`. |

### Configuration you can edit without touching code

| File | Controls |
|---|---|
| `.env` | Database connection, API key, confidence threshold, job mode. |
| `config/statement_templates.yaml` | The **line structure of every financial statement** (SFRS presentation). Add, rename or reorder lines here. |
| `config/mapping_rules_default.yaml` | ~120 rules mapping account labels to statement lines. Add rules for terms your clients use. |
| `config/report_sections.yaml` | The audit report's sections. **This is where your real 24-section template goes.** |

That last point is the important one: **your 24-page report template is a config
file, not a code change.** Send it whenever you're ready and it drops in here.

### The application code

```
app/
├── models.py            All database tables in one file
├── config.py            Reads .env
├── cli.py               flask commands
│
├── blueprints/          The web pages (routes)
│   ├── auth.py          Login / logout
│   ├── dashboard.py     Landing page + metrics
│   ├── customers.py     Client list, intake form, FY workspace
│   ├── documents.py     Upload, extraction trigger, Review & Correct, the edit API
│   ├── statements.py    Generate, edit figures, share, record approval
│   └── reports.py       Section builder, preview, PDF export
│
├── services/            The actual logic (no web code here)
│   ├── extraction/
│   │   ├── parsers.py   Excel, CSV, Word, PDF readers — deterministic
│   │   ├── ai.py        Claude extraction + account classification
│   │   ├── base.py      Number parsing, confidence scoring
│   │   └── __init__.py  Decides rules vs AI, saves results
│   ├── mapping.py       Label → statement line (rules, then AI, then human)
│   ├── statements.py    Builds each statement from verified data
│   ├── compute.py       All arithmetic and cross-statement formulas
│   ├── reports.py       Report assembly and PDF rendering
│   ├── storage.py       Safe file handling
│   ├── jobs.py          Background queue
│   └── audit.py         The audit trail
│
├── templates/           The HTML pages
└── static/              CSS and JavaScript
```

The split matters: **`blueprints/` handles the web, `services/` handles the
work.** You can change how a statement is calculated without touching a single
page, and vice versa.

---

## 4. How a document actually becomes a number in a statement

Walking one figure end to end.

**Step 1 — Upload.** The auditor drops `FY2025_Trial_Balance.xlsx` onto the
upload page and tags it *Trial Balance*. The file is renamed to a random ID on
disk (the original name is kept only in the database), checked against a size
and type allowlist, and hashed so duplicate uploads are detectable.

**Step 2 — Extract.** `openpyxl` opens the workbook, finds the header row,
works out which column is Debit and which is Credit, and reads every data row.
Each row is scored: spreadsheet data starts at 0.95 confidence, with penalties
for empty labels, unparseable amounts, or a label that's mostly digits (which
usually means columns got misaligned). A row scoring below 0.80 is flagged.

Along the way, the number parser handles what actually breaks real files:
`(1,234.00)` → −1234.00, trailing `Cr`/`Dr`, `S$` prefixes, unicode minus signs,
and `–` / `NIL` placeholders that mean "nothing" rather than zero.

**Step 3 — Review & Correct.** The auditor sees the original file on the left
and an editable grid on the right. Flagged rows are amber. Every edit saves
instantly, clears the flag, and writes an audit-log entry. A live debit/credit
footer shows whether the trial balance actually balances. **The "Mark verified"
button stays disabled while any flagged row is unresolved** — this is the gate.

**Step 4 — Map.** On generation, the row labelled `"Trade Debtors"` is matched
against the rules. A seed rule maps `trade debtor` → `trade_receivables`. If no
rule matched, Claude is asked; if Claude isn't confident, it goes to the
Unmapped tray for the auditor to assign — and **that assignment is saved as a
rule for this client**, so next year it maps automatically. The tool gets faster
the more you use it.

**Step 5 — Compute.** Statements build in dependency order because they feed
each other:

```
Trial Balance → Profit or Loss → Financial Position → Cash Flows
                      │                  ▲
                      └── profit for the year ──┘
```

The year's profit flows into retained earnings on the balance sheet. Every
formula is a named Python function (`profit_for_year`, `total_assets`, …) — no
`eval()`, nothing the AI touches. The balance sheet shows a live check of
whether total assets equal total equity and liabilities.

**Step 6 — Adjust.** The auditor can click any figure and override it. The
auto-calculated value is stored underneath, so overrides survive a regenerate
and can be reverted with one click. Overridden lines are visibly badged.

**Step 7 — Share and approve.** "Share with customer" sets the status and
timestamps it. When the client confirms, the auditor records who approved it.
That's what unlocks the report.

> **Note:** the share step is deliberately status-only. Whether customers get a
> secure link or their own login is still undecided, so nothing is emailed
> anywhere yet. The database table for approval links already exists, unused —
> so wiring up whichever mechanism you choose later is one route and one page,
> with nothing downstream affected.

**Step 8 — Report.** The report builder shows the sections on the left (toggle
on/off, drag to reorder, edit the text) and a live preview on the right.
Statement sections render the real generated figures. Export produces a PDF with
page numbers and page breaks.

---

## 5. Decisions worth knowing about

**Singapore-specific throughout.** Customer records use UEN (not the Indian
PAN/GSTIN the first draft assumed), statements follow SFRS presentation
("Statement of Financial Position", not "Balance Sheet"), currency defaults to
SGD, and the financial year end is a per-client setting because Singapore
companies choose their own.

**Comparatives are built in.** Each financial year links to the previous one, so
statements carry both years' columns — which real statutory statements require.

**Extraction is deliberately re-runnable.** Adding an API key later and clicking
"Re-extract" reprocesses a document with the better engine. Nothing is lost.

**The audit log is not optional.** Every correction, override and approval is
recorded with user, timestamp and before/after values. For audit software the
correction history is itself evidence, and it's effectively impossible to add
this retrospectively.

---

## 6. What is NOT done yet

Being explicit so nothing surprises you in the demo:

| Item | Status |
|---|---|
| **The real 24-section report** | Placeholder sections only (cover page, directors' statement, auditor's report, the statements, a couple of notes). Yours drops into `config/report_sections.yaml` when you send it. |
| **Customer approval mechanism** | Deliberately deferred, as agreed. Status-only for now; nothing is emailed. |
| **One-click PDF export on Windows** | Falls back to browser Print → Save as PDF. Works one-click on the Linux VPS once WeasyPrint is installed. |
| **The code has not been executed** | There's no Python on this machine, so nothing has been run or tested yet. Expect to hit a few small runtime errors on first launch — send me whatever the terminal says and I'll fix them. |
| **Database migrations** | Tables are created with `flask init-db`. Alembic is installed but not wired up; that matters once you have production data to preserve. |
| **Automated tests** | Not written yet. Worth adding for the number parser and the statement formulas — those are where a silent bug would be most costly. |

---

## 7. Quick demo script

A five-minute walkthrough that shows the whole product:

1. **Sign in** — `demo@auditmate.sg` / `demo1234`
2. **Dashboard** — metrics, active engagements, extraction breakdown showing
   how much AI handled versus free rule-based parsing
3. **Customers → Marina Bay Trading Pte Ltd** — Singapore client record with
   UEN, two financial years
4. **Open FY2025** — the progress rail: Documents → Statements → Approval → Report
5. **Documents** — three files in different states: a verified trial balance, a
   scanned PDF read by AI with flagged rows, and one still queued
6. **Open the scanned PDF → Review & Correct** — *this is the best part of the
   demo.* Show the amber flagged rows, correct one and watch it turn green, and
   point out that "Mark verified" is blocked until they're all resolved
7. **Statements** — six statements, SFRS presentation, prior-year column.
   Open the Statement of Financial Position and show the green balance check
8. **Override a figure** — click an amount, change it, watch the totals
   recalculate and the line get badged "overridden"
9. **Share with customer → Record approval** — watch the audit report unlock
10. **Audit Report** — toggle sections, drag to reorder, preview, export

The story to tell: *the app reads the client's documents automatically, shows
the auditor exactly what it read so they can correct it, then builds the
statements and report from data a human has signed off on.*
