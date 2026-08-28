# Beta — the preparation build

Written before implementation, against the firm's beta feedback of
28 August 2026. Nothing in here is built yet unless a stage says so.

## Why there are two versions

**v1** — tag `v1.0`, branch `main`. The audit build. Deployed, working, and
what a client demo runs against. It reconstructs a trial balance from
uploaded documents and produces an audit report.

**Beta** — branch `beta`. The preparation build. The trial balance goes in
directly and the deliverable is a set of unaudited financial statements as
an editable Word document.

They are separate branches because the change is not a refactor. The firm's
own words: *"Rebuilding it from source documents repeats work that has
already been done and paid for."* v1 must stay demonstrable while beta is
half-finished, and a half-finished beta must never reach the production
server by accident.

Merging beta into main happens once, when the firm accepts it.

## The one sentence that explains the whole change

v1 asks *"do the debits equal the credits?"* — which Xero already
guarantees, because it cannot post a one-sided entry.

Beta asks *"does the trial balance agree with everything else in the
file?"* — which is where errors actually are. On the tested client, income
tax and GST sat in operating expenses, depreciation disagreed with the
asset's useful life, and tax payable had not moved in a year. Every one of
those balanced perfectly.

---

## Stage 1 — Get the right figures in

Small, and it fixes faults that exist today.

**1.1  Identify a document by its content, not its name.**
Today the preparer picks a category from a dropdown, one per batch. A
client sends a P&L, a balance sheet and a ledger together, so one dropdown
cannot describe them and all three arrive as `other` — which builds
nothing. Reading the file name (shipped in `97ad13e`) is a half-measure;
`Report_final_v2.pdf` still says nothing. Read the extracted content: a
trial balance has paired debit and credit columns that agree, a balance
sheet has assets and no revenue, a P&L has revenue and no bank, a ledger
has thousands of dated rows. Show what was decided, let it be corrected.

**1.2  Prefer the balance sheet and P&L over the general ledger.**
`TB_SOURCE_PRECEDENCE` currently ranks the ledger second. It should rank
last. A ledger states movements, not balances, and its rows are named after
suppliers rather than accounts — which is why an engagement built from one
came out with hundreds of unmapped accounts. The balance sheet and P&L
together are already one line per account, at the year end, in account
names.

*Needs the firm's confirmation. It changes which document produces the
audited figures, which is an auditor's judgment.*

**1.3  Verify which columns we read from Xero.**
Xero's trial balance report carries a period-movement pair and a
year-to-date pair side by side. `parse_trial_balance` takes the first two
it finds. If those are movements, every pull balances to the penny and is
wrong — the same failure as the prior-year column that was silently added
into the current year. `flask xero-report` exists to answer this; it has
not been run against a report with data in it yet.

---

## Stage 2 — Mapping

The largest missing piece, and everything downstream waits on it. No
mapping, no statements. No statements, no notes. No notes, no deliverable.

The engine exists — 188 rules, per-client overrides, `match_label()`. What
is missing is the screen and the memory.

**2.1** A screen: trial balance account on the left, financial statement
line on the right, every account listed.

**2.2** Pre-filled from **last year's mapping for this client**, so the
second year opens already answered.

**2.3** Anything last year does not cover, filled from the rule library.

**2.4** Anything neither can place, flagged `unmapped` — visibly. Today an
unmapped account silently fails to reach the statements, so a new bank
account could simply be absent with nothing announcing it.

**2.5** The preparer reviews and adjusts. Account codes carry no meaning —
one tested client mixes 4000-series codes with `Exp-1`…`Exp-14`,
`CA-1`…`CA-5` and `CL-1`…`CL-3`. No rule set will ever guess those. A human
decides once.

**2.6** The mapping is saved against the client and reused every year
after. This is also a consistency requirement: if an account sits under
Employee benefits one year and Rental expense the next, the comparative
column stops being comparable.

---

## Stage 3 — Check the trial balance outward

The product's actual value. `reconcile.py` already compares documents and
reports agrees / differs / missing; it is currently pointed at the wrong
thing. Same engine, different inputs.

Each check names the evidence document, the client's figure, ours, and the
difference between them.

| Check against | What it proves |
|---|---|
| Last year's signed accounts | Every opening balance equals last year's closing. A difference means someone posted into a signed year. |
| Fixed asset register | Cost, additions, disposals; depreciation against each asset's useful life. |
| Bank statements | Cash at the year end. |
| GST returns | GST payable or receivable. |
| Tax assessment | Tax payable and tax expense. |
| Aged receivables listing | The trade receivables total. |
| Aged payables listing | The trade payables total. |
| Last year, line by line | Balances that did not move, balances that moved sharply, accounts that appeared or vanished. |

### Last year's signed accounts do four jobs, not one

Worth stating plainly because it is easy to file this document as "evidence"
and miss half of what it is for.

1. **The comparative column** — required *data*. Without it the statements
   cannot be issued at all.
2. **Opening balances tie back** — a check, and the most powerful one there
   is.
3. **Movement review** — a check.
4. **Last year's mapping** — required *data*, and what Stage 2.2 reads.

---

## Stage 4 — Say what is missing, before generating

Nothing here is technically hard and it is the highest value per hour in
the whole plan. It turns *"the Word file came out with holes"* into
*"you are missing three documents, here they are."*

Each note declares what it needs. Before anything is generated, the system
lists what it does not have, which note that blocks, and what would
complete it.

| Note | Needs |
|---|---|
| Receivables aging | Aged receivables listing |
| Fixed asset movement | Cost, purchase date, useful life |
| Every comparative column | Last year's signed accounts |
| Corporate information, directors' statement | Directors, secretary, registered office, shareholdings at both year ends |

---

## Stage 5 — Notes — **PENDING, PLACEHOLDER ONLY**

**Do not build this stage yet.**

The firm's notes library is **a separate document they maintain**, not the
25 sections in `config/report_sections.yaml`. Our sections were built from
an annual report template; theirs is their own wording, kept outside this
app. Building a library around our template would produce something they
would have to abandon.

**Blocked on:** the firm supplying that document, so its actual structure
can be read before anything is designed around it.

What is already known about the requirement, for when it unblocks:

- Editable by the firm without a developer.
- **Versioned by financial year end**, and the report picks the version
  matching the engagement's year end — never today's date. A late 2024 set
  prepared in 2026 needs the 2024 wording, because notes on newly adopted
  standards and standards not yet adopted change every year.

One piece of this stage is **not** blocked and can be done alongside Stage 2:

**5.1  Pre-tick a note when its account has a balance.** The tick list
already exists in the report builder — checkbox per section, enabled count,
drag-to-reorder, per-section editing. Only the automatic pre-ticking is
missing. Today the tick comes from a fixed `default_enabled` in config, so
every engagement opens identically regardless of what is in the trial
balance. A company with no bank loan should not get a borrowings note.

*The feedback records "nothing at this layer" for note selection. That is
not correct — the screen exists. Worth confirming the firm opened it.*

---

## Stage 6 — Output

**6.1** Remove the Independent Auditor's Report section. Removing it is
what makes the set unaudited. One section, and the meaning of the whole
document changes.

**6.2** Produce an editable Word document. Today we produce PDF and Excel
and no Word at all. "Editable" is the point — the preparer finishes it by
hand, changes figures, adds a note that was never in the list.

The document's structure is already right. What
`config/report_sections.yaml` holds today:

```
Cover Page and Corporate Information
Directors' Statement
Independent Auditor's Report          <- the only thing that must go
Statement of Comprehensive Income
Statement of Financial Position
Statement of Changes in Equity
Statement of Cash Flows
Notes 1 to 17
Detailed Profit and Loss Statement
```

Against the firm's words: *"cover, corporate information, contents,
directors' statement, the four primary statements with comparatives and
note references, notes 1 to 17, and the detailed profit and loss statement
at the end."* The same document, down to the count of notes.

---

## Stage 7 — Removals

**7.1  The engagement bar.** The Customers screen already lists every
client with name, UEN, entity type, contact and financial years, with
search and an Open button. *"You can see what is there instead of guessing
at it."*

**7.2  Client approval.** *"The SME client is not an accountant and will
not log in to approve accounts."* The firm emails the draft; the client
replies. This retires working software — the review link, the passcode, the
Excel round trip, `versions.py`. The firm leaves one door open: a portal is
acceptable **for clients to send documents in**, never for approval. That
is where this should move rather than be deleted.

---

## Open questions

**1. Clients who are not on accounting software.**
Stage 1 assumes a trial balance exported from Xero or QuickBooks. For
Singapore SMEs that is not always true. Either the build-from-documents
path stays as a fallback, or the firm does not take that engagement into
AuditMate. The answer decides how much of the v1 extraction work carries
over, so it is worth settling before Stage 1 rather than after.

**2. Precedence — Stage 1.2.** Confirmation that the balance sheet and P&L
should outrank the general ledger as the build source.

**3. The notes document — Stage 5.** Needed before that stage can start.

## Deployment

v1 is what `/opt/auditmate/app` runs, from `main`. Beta must not be pulled
onto that server while it is half-finished. When beta is ready to be shown,
it needs its own port, its own database and its own service — the VPS is
shared with roughly twenty other live projects, so that has to be
deliberate rather than incidental.
