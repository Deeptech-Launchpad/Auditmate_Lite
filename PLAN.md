# Auditmate Lite — Phase 1 Implementation Plan

**Status:** Draft for approval · **Date:** 2026-08-22
**Stack:** Python 3.12 · Flask · PostgreSQL 16 · Jinja2 + vanilla JS (no build step) · local disk storage

---

## 0. Analysis — what this project actually is

Stripped to its core, this is a **four-stage pipeline** with a human checkpoint at each stage:

```
Documents  ──extract──▶  Line Items  ──map──▶  Statements  ──approve──▶  Audit Report
   (raw)      (auto)      (verified)   (auto)    (adjusted)    (locked)     (PDF)
              ▲ REVIEW              ▲ EDIT                  ▲ CUSTOMER
```

Two observations drive most of the design decisions below:

1. **Extraction is the risk centre.** Everything downstream is deterministic data
   transformation; only the extraction stage deals with messy reality (scanned PDFs,
   inconsistent Excel layouts, merged cells). The plan therefore treats extraction as a
   *pluggable pipeline with confidence scoring*, not a single function, and the
   Review & Correct screen as a first-class module rather than a form.

2. **This is an audit tool, so provenance matters more than it would elsewhere.**
   Every number in a final statement must be traceable back to a cell in a source file,
   and every auditor override must be attributable. This adds two things not in the
   original brief that I recommend including: a `source_ref` on every extracted line item,
   and an `AuditLog` table recording every correction. Both are cheap now and effectively
   impossible to retrofit.

### Gaps found in the brief

| # | Gap | How the plan handles it |
|---|-----|------------------------|
| G1 | No mapping layer between extracted line items and statement lines. The brief says statements "auto-populate from verified documents" but doesn't say *how* a row labelled `"Sundry Debtors"` becomes the Balance Sheet line `trade_receivables`. | New `AccountMapping` model + rules engine (§3.4). Learns from auditor corrections per customer. |
| G2 | Statements depend on each other (P&L → BS) but there is no defined calculation order. | Explicit `compute.py` with a fixed dependency order and named formulas (§3.5). |
| G3 | OCR is slow (10–60s per page). Synchronous upload requests will time out. | DB-backed job queue using `SKIP LOCKED`, separate worker process, UI polls status (§3.3). |
| G4 | No comparative-period handling. Every real financial statement shows current *and* prior year side by side. | `FinancialYear.previous_year_id` plus `amount_previous` on every statement line (§3.1). This also answers the brief's open item on carry-forward. |
| G5 | Uploaded files are untrusted binaries fed to parsers with known CVE history (zip bombs in `.xlsx`, XXE in `.docx`, malformed PDFs). | Hardening measures in §6. |
| G6 | No local dev environment exists (no Python, no Postgres, no Tesseract). | Prerequisites in §7.1 — **this blocks step one.** |

---

## 1. Architecture

Flask **application factory** plus **blueprints**, SQLAlchemy 2.x ORM, Alembic migrations,
Flask-Login sessions, Flask-WTF CSRF. Server-rendered Jinja2 templates; JavaScript used only
where genuinely interactive (the review grid, upload dropzone, job polling) and written as
plain ES modules with no bundler — matching the existing AltiusNXT deployment style.

```
auditmate/
├── wsgi.py                     gunicorn entrypoint
├── worker.py                   background extraction worker (separate process)
├── requirements.txt
├── .env.example
├── config/
│   ├── statement_templates.yaml   canonical line keys per statement type
│   ├── mapping_rules_default.yaml seed label→line rules
│   └── report_sections.yaml       the 24 report sections
├── migrations/                 alembic
├── storage/                    uploaded files (gitignored, outside webroot)
├── app/
│   ├── __init__.py             create_app()
│   ├── config.py               Dev / Prod / Test config classes
│   ├── extensions.py           db, migrate, login_manager, csrf, limiter
│   ├── models/                 one module per aggregate
│   ├── blueprints/
│   │   ├── auth/               login, logout, password change
│   │   ├── dashboard/          metrics landing page
│   │   ├── customers/          CRUD + detail + financial years
│   │   ├── documents/          upload, list, review & correct
│   │   ├── statements/         build, edit, preview, share
│   │   ├── reports/            section builder, preview, PDF export
│   │   └── api/                JSON endpoints (grid saves, job polling)
│   ├── services/
│   │   ├── storage.py          safe path building, streaming download
│   │   ├── extraction/
│   │   │   ├── base.py         ExtractionResult dataclasses
│   │   │   ├── dispatch.py     file-type → extractor
│   │   │   ├── excel.py  csv_.py  docx_.py  pdf_.py  ocr.py
│   │   │   ├── normalize.py    number/label cleanup, sign detection
│   │   │   └── confidence.py   scoring rules
│   │   ├── mapping.py          line item → statement line
│   │   ├── statements.py       build each statement type
│   │   ├── compute.py          derived + cross-statement formulas
│   │   ├── reports.py          section registry + rendering
│   │   ├── pdf.py              HTML → PDF (pluggable backend)
│   │   ├── jobs.py             enqueue / claim / complete
│   │   └── audit.py            audit-log helper
│   ├── templates/
│   └── static/css,js
└── tests/
```

**Why a separate worker process:** running the extraction pool inside the Flask process breaks
under gunicorn's multiple workers — each would start its own pool and process the same job N
times. A `Job` table claimed with `SELECT … FOR UPDATE SKIP LOCKED` gives safe coordination
with zero extra infrastructure: no Redis, no Celery. A `JOBS_INLINE=1` dev flag runs jobs
synchronously so a single `flask run` is enough for local work.

---

## 2. Data model

Additions beyond the brief are marked **(+)** with the reason.

### User
`id, name, email(unique), password_hash, role(admin|auditor), is_active, created_at, last_login_at`

### Customer
Framework confirmed: **Singapore** (ACRA / Companies Act, SFRS statement formats). Intake set:

`id, name, legal_name, entity_type(sole_proprietorship|partnership|llp|private_limited|public_limited|branch|other),`
`uen, incorporation_date, financial_year_end_month,`
`email, phone, contact_person,`
`address_line1, address_line2, postal_code, country(default Singapore),`
`books_currency(default SGD), engagement_partner_id→User, notes,`
`is_active, created_at, updated_at, created_by`

(`uen` = Unique Entity Number, replaces the PAN/GSTIN/CIN fields from the original India-assumption
draft. GST registration number can be added as an optional field if any customers are
GST-registered — confirm if needed.)

### FinancialYear
`id, customer_id, year_label("2025-26"), start_date, end_date,`
`status(in_progress|statements_shared|approved|report_generated|closed),`
**(+)** `previous_year_id→FinancialYear` — comparatives and opening balances (gap G4),
`created_at, updated_at` · unique `(customer_id, year_label)`

### Document
`id, financial_year_id, original_filename, stored_filename, storage_path,`
`file_type, mime_type, size_bytes,` **(+)** `sha256` (duplicate-upload detection),
`category, page_count,`
`extraction_status(queued|processing|extracted|failed), extraction_engine, extraction_error, extraction_confidence,`
`review_status(pending|in_review|verified|rejected),`
`uploaded_by, uploaded_at, reviewed_by, reviewed_at`

### ExtractionRun **(+)**
Keeps re-runs auditable and lets you re-extract with a better engine without losing history.
`id, document_id, engine, status, started_at, finished_at, raw_payload(JSONB), error`

### ExtractedTable **(+)**
`id, document_id, extraction_run_id, sheet_or_page, table_index, header(JSONB), bbox(JSONB), row_count, col_count`

### ExtractedLineItem
The unit the Review & Correct screen edits.
`id, document_id, extracted_table_id, row_index,`
`raw_label, raw_values(JSONB), label, account_code, amount, debit, credit, period(current|previous),`
`confidence(0–1), needs_review(bool),`
**(+)** `source_ref(JSONB {sheet,cell,page,bbox})` — provenance, click-to-highlight in the viewer,
`status(auto|corrected|accepted|discarded), corrected_by, corrected_at`

### AccountMapping **(+)** — closes gap G1
`id, customer_id(NULL = global rule), pattern, match_type(exact|contains|regex),`
`statement_type, line_key, sign(+1|-1), priority, source(seed|learned|manual), created_by, created_at`

### FinancialStatement
`id, financial_year_id, statement_type, status(draft|shared|approved), version,`
`generated_at, shared_at, approved_at, approved_by_name, approved_note, notes`

### StatementLine **(+)**
The brief offered "structured JSON or line-item table". A table is the right call — it makes
recomputation, provenance and comparatives queryable instead of opaque.
`id, financial_statement_id, line_key, label, group_key, sort_order,`
`amount_current, amount_previous, is_subtotal, is_computed, formula,`
`source(auto|manual|computed), manual_override_amount, source_line_item_ids(JSONB)`

### AuditReport
`id, financial_year_id, title, status(draft|final), version, generated_at, generated_by, pdf_path`

### AuditReportSection **(+)**
Sections need editable per-engagement text, not just a key list.
`id, audit_report_id, section_key, title, sort_order, is_enabled, content_html, data_binding(JSONB)`

### CustomerApprovalLink — schema only in phase 1
`id, financial_year_id, token_hash, created_by, expires_at, used_at, revoked_at, created_at`
The table is created but no customer-facing route is wired; the approval decision stays deferred.

### AuditLog **(+)** — non-negotiable for an audit product
`id, user_id, entity_type, entity_id, action, before(JSONB), after(JSONB), ip, created_at`

---

## 3. Module designs

### 3.1 Auth and layout
Argon2 password hashing, Flask-Login sessions, `HttpOnly`/`Secure`/`SameSite=Lax` cookies,
rate-limited login (5 attempts per 15 minutes per IP). Base template with a persistent left
sidebar (Dashboard, Customers) and a breadcrumb strip showing `Customer › FY › Stage` once
inside a financial-year workspace. No self-registration — auditors are seeded by an admin CLI
command.

### 3.2 Customers and financial years
List with server-side search and pagination. The add-customer form creates the first
`FinancialYear` in the same transaction. Customer detail shows financial years as cards with a
status pill and a progress strip (Documents → Statements → Report), each linking into the FY
workspace.

### 3.3 Documents and extraction

**Upload:** drag-and-drop multi-file, per-file category selector, client-side size/extension
pre-check, server-side validation (§6). Files stored at
`storage/<customer_id>/<fy_id>/<uuid>__<sanitized_name>` — never served statically, always
streamed through an authenticated route.

**Pipeline:** each upload enqueues a `Job`; the worker claims it and dispatches by type. Per the
"Rules + LLM fallback" decision, deterministic parsers run first (fast, free, exact on clean
files); Claude is called only when the deterministic pass is low-confidence or the file type
has no reliable rule-based path:

| Type | Primary (rules) | Fallback | Base confidence |
|------|---------|----------|-----------------|
| `.xlsx` / `.xls` | openpyxl | pandas | 0.95 |
| `.csv` | pandas (sniffed dialect) | — | 0.95 |
| `.docx` | python-docx (text + tables) | Claude (doc as text) | 0.85 |
| `.pdf` typed | pdfplumber (tables + words) | Claude (PDF sent as a native `document` block) | 0.75 |
| `.pdf` scanned / image | — | Claude (PDF/image sent directly — no local OCR step) | n/a → LLM |

Claude Opus 5 accepts PDFs natively as base64 `document` content blocks (up to 600 pages) and
reads embedded page images directly — this replaces a separate OCR step for scanned documents
rather than sitting behind one. The extraction call uses `client.messages.parse()` with a
Pydantic schema (`ExtractedLineItem` list: label, amount, debit, credit, period, row/cell
reference) so the response is validated structured JSON, not text to re-parse. `citations:
{enabled: true}` is turned on for PDF documents so each returned line item carries a
`page_location` back to the source page — this is what feeds `source_ref` for the click-to-
highlight provenance link in Review & Correct. Model: `claude-opus-5`, extraction runs are
short single calls so a per-call token budget is unnecessary; `output_config.effort` set to
`medium` (structured extraction doesn't need `high`/`xhigh` reasoning depth). Tesseract/Poppler
are still installed as an **offline fallback** (§7.1) for when the API is unreachable or a
firm-level policy wants zero-egress processing for a given document — not the primary path.

**Normalization** (`normalize.py`) handles the things that actually break real files, run after
either extraction path: parenthesised negatives `(1,234)`, trailing `Cr`/`Dr`, `S$`/`SGD`
currency prefixes, unicode minus, blank-versus-zero, merged header cells, multi-row headers.
Singapore filings commonly use comma-grouped thousands (`1,234,567.00`) — same parser as a
generic Western format, no special-casing needed (unlike the Indian lakh/crore grouping this
would have required under the original assumption).

**Confidence** (`confidence.py`) starts from the engine base score and adjusts:
`-0.20` unparseable amount · `-0.15` empty label · `-0.10` Claude reports low certainty for a
row (the extraction schema includes a per-row `confidence` field the model fills in) ·
`+0.10` row reconciles to a column subtotal · `-0.30` trial balance where Σdebit ≠ Σcredit.
Anything below **0.80** is flagged `needs_review=true`. Every row is reviewable regardless of
source — rule-extracted and LLM-extracted rows go through the identical Review & Correct grid.

**Review & Correct screen** — split pane, resizable:

- *Left:* the original file. PDFs via PDF.js with the source cell highlighted on row focus;
  Excel/CSV rendered as a read-only HTML table; DOCX as formatted text.
- *Right:* an editable grid of extracted line items. Low-confidence rows tinted amber, failed
  rows red. Inline edit of label/amount/debit/credit, row discard, add-missing-row.
  Live footer totals with a Σdebit = Σcredit check for trial balances.
- Keyboard-first (Tab/Enter/arrow navigation) — auditors will process hundreds of rows.
- Autosave per cell via the JSON API; every edit writes an `AuditLog` entry and flips the row
  to `status=corrected`.
- **Mark Verified** is blocked while any row is still `needs_review` and untouched.

### 3.4 Mapping layer (closes G1)
On verification, each line item is matched against `AccountMapping` rules — customer-specific
rules first, then global seeds from `mapping_rules_default.yaml`, highest priority wins.
Unmatched items land in an **Unmapped items** tray on the statement screen, where the auditor
assigns a target line once; that choice is saved as a customer-scoped `learned` rule, so the
same customer's next year maps automatically. This is the mechanism that makes the tool get
faster with use rather than staying constant.

### 3.5 Financial statements
Line keys per statement type come from `config/statement_templates.yaml`, seeded to Singapore
Financial Reporting Standards (SFRS) presentation — "Statement of Financial Position" rather
than "Balance Sheet", "Statement of Profit or Loss" rather than "P&L", SGD as the default
currency, current/non-current split on both assets and liabilities. Because line keys live in
config rather than code, this is a data change, not an architecture one, if a customer needs a
different presentation (e.g. a branch reporting under a different framework). Build order is
fixed because of the inter-statement dependencies (G2):

```
1. Trial Balance          ← mapped line items (Σdebit must equal Σcredit)
2. P&L                    ← TB revenue/expense accounts
3. Balance Sheet          ← TB asset/liability accounts + P&L net result → retained earnings
4. Accounts Receivable    ← receivables-category documents
5. Accounts Payable       ← payables-category documents
6. Cash Flow (indirect)   ← P&L net result + BS movements vs previous year
```

Each line carries `amount_current` and `amount_previous` (from `previous_year_id`, blank for a
first year). Formulas are named functions in `compute.py` — **never `eval()`**. The editable
preview lets the auditor override any figure; an override sets `manual_override_amount`, marks
the line `source=manual`, and survives recomputation, with a visible "overridden" badge and a
one-click revert. Each statement has a print-styled preview route that the PDF renderer reuses
verbatim.

### 3.6 Share / approval (deliberately minimal)
`Share with customer` sets every statement to `shared` and the FY to `statements_shared`,
stamping `shared_at`. An auditor-only `Record approval` action sets `approved` plus
`approved_at` and a free-text `approved_by_name`. The `CustomerApprovalLink` table exists but
is unwired. The audit-report module is gated on FY status `approved`. When the real mechanism
is chosen, only one route and one template need to be added — nothing downstream changes.

### 3.7 Audit report
**Status: template pending.** You'll provide the actual 24-section report — until it's in
hand, M6 builds the *section engine* (the three types below, the drag-to-reorder builder, the
preview/PDF pipeline) against a **placeholder set of 3–4 sections** so the mechanism is provable
end-to-end, without guessing at content that would just get thrown away. `report_sections.yaml`
is where the real template gets authored once you send it — send the file, a shared doc, or a
paste of the section list/headings, and that becomes a config change, not a rebuild. Sections
are declared like:

```yaml
- key: independent_auditors_report
  title: "Independent Auditor's Report"
  type: template          # template | free_text | statement
  default_enabled: true
  bindings: [customer, financial_year]
```

Three section types: `statement` (renders a FinancialStatement), `template` (boilerplate with
`{{ customer.name }}`-style bindings, auditor-editable), `free_text` (rich-text commentary).
The builder screen is a drag-to-reorder checklist on the left with a live preview on the right.
Export renders the assembled HTML to PDF with page numbers, header/footer and a table of
contents. `services/pdf.py` exposes one interface with two backends: **WeasyPrint** (default,
Linux VPS) and **Playwright/Chromium** (fallback, works on Windows dev without GTK).

---

## 4. Milestones

| # | Milestone | Deliverables | Est. |
|---|-----------|--------------|------|
| **M0** | Scaffolding | App factory, config, extensions, Alembic, `.env.example`, seed CLI, README, base layout + sidebar CSS | 1 d |
| **M1** | Auth + dashboard | Login/logout, session guard, rate limit, dashboard with the five metrics | 1 d |
| **M2** | Customers + FY | Customer CRUD, search, detail view, FY create/switch, workspace shell | 1.5 d |
| **M3** | Documents + extraction | Upload, storage, type detection, all five extractors, normalize/confidence, job queue + worker, document list, **Review & Correct split-pane grid** | 4–5 d |
| **M4** | Statements | Mapping engine + seed rules, six statement builders, compute/derived fields, editable preview, print view, unmapped tray | 3–4 d |
| **M5** | Share / approval | Status transitions, share + record-approval actions, FY status gating, approval-link schema stub | 0.5 d |
| **M6** | Audit report | Section registry + config, builder UI, live preview, PDF export with TOC | 2–3 d |
| **M7** | Hardening | Upload security, tests, deployment notes (gunicorn + systemd + nginx), backup guidance | 1.5 d |

**Total ≈ 15–18 working days.** M3 and M4 carry essentially all the risk; M0–M2 and M5 are routine.

---

## 5. Testing

- **Unit:** `normalize.py` number parsing (Indian grouping, negatives, Cr/Dr) — table-driven;
  `compute.py` formulas against hand-worked figures; mapping rule precedence.
- **Fixtures:** a `tests/fixtures/` set of deliberately awkward files — merged-cell Excel,
  multi-row headers, a rotated scan, a PDF with a table split across pages — each with an
  expected-output JSON. This suite is what tells you whether an extraction change helped or hurt.
- **Integration:** upload → extract → verify → statement → report happy path.
- **Security:** oversized upload, disguised extension, path traversal in filename, zip bomb,
  cross-customer access attempt (auditor A opening customer B's document by ID).

---

## 6. Security

Uploaded files are untrusted input fed to parsers with real CVE history, so:

- Extension **and** content-sniffed MIME must both be in the allowlist; reject on mismatch.
- 25 MB per file / 200 MB per request cap, enforced by `MAX_CONTENT_LENGTH` *and* nginx.
- Filenames sanitized and replaced by a UUID on disk; the original is kept in the DB only.
  Storage root resolved and asserted with `Path.resolve().is_relative_to(ROOT)`.
- `defusedxml` for all OOXML parsing; a decompressed-size cap against zip bombs.
- Page-count and wall-clock timeouts on PDF/OCR jobs; the worker runs unprivileged.
- Every document/statement/report route re-checks the object's customer scope — never trust an
  ID in the URL.
- CSRF on all state-changing forms; Argon2 hashing; rate-limited login; security headers and a
  CSP that permits only self-hosted assets.
- Downloads streamed through an auth-checked route with `Content-Disposition: attachment` and
  `X-Content-Type-Options: nosniff`.

---

## 7. Prerequisites and assumptions

### 7.1 Environment — **blocks M0**
This machine has no usable Python (only the Microsoft Store alias stub), no PostgreSQL. Before
implementation can run anything, we need Python and PostgreSQL at minimum; Tesseract/Poppler
are lower priority now that extraction routes scanned documents to Claude directly (§3.3) —
they're installed as an offline fallback, not a blocker for M3.

| Component | Version | Note | Blocking? |
|-----------|---------|------|-----------|
| Python | 3.12.x | from python.org, **not** the Store stub | Yes — blocks everything |
| PostgreSQL | 16 | local instance plus a dev database | Yes — blocks M2 onward |
| `ANTHROPIC_API_KEY` | — | needed for the extraction LLM fallback | Yes — blocks M3 extraction path (rule-based parsing still works without it) |
| Tesseract OCR | 5.x | offline fallback only, needs `TESSDATA_PREFIX` | No — nice to have |
| Poppler | latest | offline fallback only (`pdf2image`) | No — nice to have |

See `SETUP.md` for the exact install commands (winget-based) — run that first, then M0 starts.

### 7.2 Assumptions to confirm

| Ref | Assumption | Impact if wrong |
|-----|-----------|-----------------|
| A1 | ~~Indian statutory context~~ — **confirmed: Singapore** (ACRA/UEN, SFRS statement formats, SGD, FYE-month-driven year labels) | Resolved — see §2 Customer, §3.5 |
| A2 | Document categories: balance_sheet, trial_balance, bank_statement, vendor_invoice, customer_invoice, salary_schedule, payables, receivables, fixed_asset_register, tax_document, other | Enum plus seed mapping rules |
| A3 | Prior-year figures shown as comparatives; opening balances carried from the linked previous FY | Adds `previous_year_id`; already in the model |
| A4 | Report sections: **template pending** — M6 builds the section engine against a small placeholder set until the real 24-section spec is provided (§3.7) | Once received, becomes a config-only change to `report_sections.yaml` |
| A5 | Single-tenant deployment (one audit firm), auditors see all customers | Multi-tenant would need a firm/org scope on every table — far cheaper to decide now |
| A6 | ~~Extraction is rule/heuristic based~~ — **confirmed: rules + Claude API fallback** (§3.3) | Resolved |

---

## 8. Immediate next steps on approval

1. Run `SETUP.md` — installs Python 3.12, PostgreSQL 16 (winget), creates the dev DB, and
   collects an `ANTHROPIC_API_KEY`.
2. M0 scaffolding, first migration, seed auditor account.
3. M1 auth and dashboard shell — the first runnable, clickable slice.
4. When you have the 24-section report template, send it — it slots into M6 as a config file,
   no rework of earlier milestones needed.
