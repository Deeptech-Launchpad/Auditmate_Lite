# Auditmate Lite — Phase 2 Implementation Plan

**Status:** Draft for approval · **Date:** 2026-08-22
**Nothing in this plan has been built. No code changed.**

---

## 1. The flow you asked for

```
┌──────────────────────── DATA IN ─────────────────────────────┐
│                                                              │
│   Xero API      QuickBooks API      Tally        Manual      │
│   (OAuth)         (OAuth)         (see §6)       upload      │
│      │               │               │             │         │
└──────┴───────────────┴───────────────┴─────────────┴─────────┘
                              │
                              ▼
              ╔═══════════════════════════════╗
              ║   STANDARD TRIAL BALANCE      ║   one canonical TB
              ║   (Auditmate's own format)    ║   merged from every
              ║   debits must equal credits   ║   source, must balance
              ╚═══════════════════════════════╝
                              │
                   auditor reviews and locks
                              │
                              ▼
                 ✉  EMAIL A SECURE LINK
                    (no login, no portal account)
                              │
                              ▼
        ┌─────────────────────────────────────────────┐
        │  Customer opens the link                    │
        │  → sees ONLY their trial balance            │
        │  → edits figures / adds comments            │
        │  → clicks Submit                            │
        └─────────────────────────────────────────────┘
                              │
                              ▼
                 returns as TB Version N
                              │
                              ▼
        ┌─────────────────────────────────────────────┐
        │  Auditor reviews the customer's changes     │
        │  line by line, and ACCEPTS or REJECTS each  │
        └─────────────────────────────────────────────┘
                              │
                              ▼
                    FINAL TRIAL BALANCE
                              │
                              ▼
              financial statements (generated)
                              │
                              ▼
                    AUDIT REPORT (PDF)
```

### What this changes from today

| | Today | Phase 2 |
|---|---|---|
| **Data in** | Manual upload only | API pull from accounting software **plus** manual upload |
| **Central artefact** | Extracted line items feed statements directly | An explicit **Standard Trial Balance** every source merges into |
| **What the customer reviews** | The financial statements | The **trial balance** — earlier, closer to source |
| **How they review it** | Excel workbook by email | **Secure web link**, edit in the browser, no login |
| **Auditor's response** | Changes applied wholesale | **Accept or reject each change individually** |

The two structural wins: the trial balance becomes the single source of truth
that everything reconciles against, and for API-connected clients the riskiest
part of the pipeline — reading numbers out of documents — largely disappears,
because the figures arrive structured and exact.

---

## 2. Data model additions

```
Customer
  └── Connection            one per accounting system per customer
        └── SyncRun         each pull, with what came back

FinancialYear
  ├── TrialBalanceAccount   THE standard trial balance (one row per account)
  ├── TrialBalanceVersion   snapshot per review round
  │     └── TrialBalanceChange   customer's proposed edits, each accept/reject
  └── CustomerReviewLink    the emailed token
```

**Connection** — `customer_id, provider (xero|quickbooks|tally|manual),
status, external_tenant_id, access_token_enc, refresh_token_enc, expires_at,
connected_by, connected_at, last_sync_at, last_error`

Tokens encrypted at rest with Fernet, key from `.env`. A stolen database must
not hand over live access to a client's accounting system.

**SyncRun** — `connection_id, financial_year_id, started_at, finished_at,
status, accounts_pulled, raw_payload, error`. Keeps every pull auditable, and
lets you re-run one without losing the previous result.

**TrialBalanceAccount** — the canonical row:
`financial_year_id, account_code, account_name, standard_key, debit, credit,
source (xero|quickbooks|tally|upload|adjustment), source_document_id,
source_ref, confidence, needs_review, is_adjustment, status`

`standard_key` is Auditmate's own account identifier — what the existing
mapping rules already resolve to. Every source normalises into it, which is
what makes "our standard trial balance" real rather than nominal.

**TrialBalanceVersion** — same pattern as the existing `StatementVersion`:
`version_no, source (auditor|customer), status (draft|sent|customer_submitted|
final), snapshot JSON, sent_at, submitted_at, submitted_from_ip`

**TrialBalanceChange** — one per figure the customer moved:
`version_id, tb_account_id, field, value_before, value_after, customer_comment,
status (pending|accepted|rejected), decided_by, decided_at, decision_note`

This is what makes "select the portion which is correct" work — the auditor
sees a diff and rules on each line, exactly like reviewing a set of proposed
edits.

**CustomerReviewLink** — replaces the stubbed `CustomerApprovalLink`:
`financial_year_id, token_hash, passcode_hash, expires_at, revoked_at,
used_at, access_count, last_accessed_at, last_accessed_ip, created_by`

---

## 3. The secure link — design and honest risk

The link is a **bearer credential**: whoever holds the URL can see that
client's trial balance. That is the trade-off for "no login", and it needs
saying plainly, because a forwarded email hands over access.

Controls I would build in:

- **Token**: `secrets.token_urlsafe(32)` — 256 bits, not guessable.
- **Stored hashed** (SHA-256). The raw token exists only in the email. A
  database leak does not yield working links.
- **Expiry** — default 30 days, configurable, shown on the page.
- **Revocable** — auditor can kill a link instantly.
- **Scoped** — one link opens one engagement's trial balance and nothing else.
  No navigation, no other customers, no app chrome.
- **Rate limited** on the token route, to blunt brute force.
- **Every access logged** — timestamp, IP, user agent — into the audit trail.
- **HTTPS mandatory** in production (the VPS already needs a certificate).
- **Optional 4–6 digit passcode**, sent separately (SMS/phone/second email).
  Recommended: it turns a forwarded link from a breach into a nuisance.

The customer page carries no login, no password reset, no account — nothing
that could become an authentication surface.

---

## 4. Build phases

Sequenced so each phase is independently useful, and the flow change lands
before the integrations.

### Phase A — Standard Trial Balance *(foundation, ~3–4 days)*

Everything else depends on this.

- `TrialBalanceAccount` model + merge logic (same account arriving from two
  sources, dedupe, precedence rules)
- Build the standard TB from existing verified documents
- TB workspace screen: editable grid, source column, running debit/credit
  totals, balance check
- Auditor adjustment entries (journal adjustments on top of source data)
- Statements switch to reading from the standard TB rather than from extracted
  line items directly
- `flask check-config` extended to validate TB ↔ statement mapping coverage

**Deliverable:** one canonical, balancing trial balance per engagement, built
from what the app can already ingest.

### Phase B — Link review + selective acceptance *(~3–4 days)*

- `CustomerReviewLink` with the §3 controls
- Public route `/review/<token>` — standalone page, no app navigation
- Customer-facing editable TB grid: change a figure, add a comment per line,
  submit
- Submission creates `TrialBalanceVersion` + `TrialBalanceChange` rows
- Email to the customer containing the link (reusing the working Gmail setup)
- Notification back to the auditor on submission
- **Auditor diff screen** — each customer change shown side by side with
  Accept / Reject / Accept-with-edit, plus bulk actions
- Accepted changes apply to the standard TB; rejected ones are recorded with a
  reason and remain visible

**Deliverable:** the complete new flow, working end to end, with manual upload
as the data source.

### Phase C — Xero *(~3–4 days + registration lead time)*

- OAuth 2.0 (PKCE), connect/disconnect UI on the customer record
- Tenant selection (one Xero login can hold several organisations)
- Pull the Trial Balance report for the financial year's date range
- Map Xero accounts → `standard_key`, reusing the existing rules and the
  learned per-customer mappings
- Token refresh (Xero access tokens last 30 minutes, refresh tokens 60 days)
- Re-sync with a diff against the previous pull

### Phase D — QuickBooks Online *(~2–3 days)*

Same shape as Xero once Phase C exists — OAuth 2.0, `realmId`, the
`TrialBalance` report endpoint. Most of the work is the provider adapter; the
surrounding plumbing is already built by then.

### Phase E — Tally *(~4–5 days, and genuinely different — see §6)*

---

## 5. What the integrations actually require from you

Not just code. Both cloud providers need a registered developer app before a
single call can be made:

| | Xero | QuickBooks Online |
|---|---|---|
| Register at | developer.xero.com | developer.intuit.com |
| You receive | Client ID + Secret | Client ID + Secret |
| Redirect URI | must be pre-registered (your VPS domain) | same |
| Sandbox | immediate | immediate |
| **Production** | app review required | app review required |

Development can start on sandbox credentials right away. **Production access
needs their review process**, which is a business step measured in weeks, not
a coding task. Worth starting the registrations early if you want this live
soon.

---

## 5a. What an API gives you, and what it cannot

Worth stating plainly, because "connect to Xero" is easily heard as "we no
longer need documents". Both channels are permanent.

**Xero / QuickBooks can supply:**

- The trial balance itself — exact figures, no extraction, no misread risk
- Chart of accounts with codes
- Invoices, bills, credit notes, journals
- Bank *transactions* as recorded in the client's books
- Attachments the client added to records inside the system

**They can never supply:**

- **The bank's own statement.** These systems hold a bank *feed* — transaction
  data — not the bank's signed PDF. Not the same document.
- IRAS notices of assessment
- Signed contracts, leases, board minutes, directors' resolutions
- Bank confirmations sent directly to the auditor
- Anything the client never entered into the system

**The audit reason this matters.** Third-party evidence outranks
client-generated evidence. An API pull tells you *what the client recorded*;
it cannot corroborate that the record is true, because the client controls
that system and does not control the bank. So obtaining a bank statement
independently and checking it against the books is the point of the exercise,
not a workaround for a missing integration.

```
Xero API  ->  the client's books       fast, exact, no extraction risk
Upload    ->  independent evidence     bank statements, confirmations,
                                       tax notices, contracts, minutes
                    |
                    v
         both merge into the Standard Trial Balance
```

Manual upload therefore stays a first-class input channel permanently. It is
not a fallback for clients without an integration.

---

## 6. Tally — the honest problem

Tally is not like Xero and QuickBooks, and I don't want to plan as if it were.

**Tally Prime is desktop software with no cloud API.** It exposes an XML-over-
HTTP interface on port 9000 when "Act as Server" is enabled — but only on the
local network where Tally is running. There is no internet endpoint for your
VPS to call.

Three realistic options:

| Option | How it works | Verdict |
|---|---|---|
| **A. File export** | Client exports the trial balance from Tally (XML/Excel) and uploads it, or emails it | **Recommended first.** Works today with what's already built. Zero new infrastructure. |
| **B. Local connector** | A small Auditmate agent installed on the client's Tally machine, polls Tally's XML interface and pushes to your VPS | Proper automation, but it's a separate installable product with its own updates, support and security surface |
| **C. Port forwarding / VPN** | Expose the client's Tally to the internet | **Don't.** Insecure and unmaintainable across many clients |

My recommendation: treat Tally as **Option A** in Phase E — a well-supported
import path with a Tally-specific parser that understands its export format —
and only build the connector (Option B) if enough clients justify a separate
installable agent.

The same applies to other Singapore desktop packages your clients may use —
AutoCount, SQL Account, MYOB desktop. Cloud packages (Xero, QuickBooks Online,
Zoho Books, Financio) can be API-connected; desktop ones generally cannot.

---

## 7. Decisions confirmed (2026-08-22)

1. **Scope: Phases A + B first.** Integrations follow once the flow is proven.

2. **The trial balance is the single customer checkpoint.** Confirmed chain:

   ```
   Standard Trial Balance
        → customer reviews and agrees it (secure link)
        → TB APPROVED
        → financial statements generated FROM the approved TB
        → audit report generated from those statements
   ```

   Nothing downstream is produced until the trial balance is approved. There
   is no second customer review of the statements — they follow automatically
   from the agreed TB. This also means the report gate moves: today it waits
   on approved statements, in Phase B it waits on an **approved trial
   balance**.

3. **Xero is the first integration** (Phase C, after A and B).

4. **The customer link needs no credentials of any kind.** No login, no
   account, no portal. The random token in the URL is the only secret, and the
   customer never types it — they click the emailed link and land straight on
   their trial balance. The optional passcode from §3 is built but **off by
   default**, available per engagement if a particular client warrants it.
   Expiry, revocation, rate limiting and access logging apply either way.

5. **The existing Excel round-trip stays** as a fallback for customers who
   would rather work in a spreadsheet. The link becomes the default path.

---

## 8. Effort and sequencing

| Phase | Scope | Estimate |
|---|---|---|
| A | Standard Trial Balance | 3–4 days |
| B | Link review + selective accept | 3–4 days |
| C | Xero | 3–4 days |
| D | QuickBooks Online | 2–3 days |
| E | Tally (file import) | 2–3 days |
| **Total** | | **13–18 days** |

**A and B together deliver the flow you described.** C, D and E each add a
data source without changing the flow, so they can land one at a time, in
whatever order matches your clients.

Given your token budget, I'd suggest approving **A + B first** and treating the
integrations as separate pieces of work once the new flow is proven.

---

## 9. Two things worth flagging

**Confidentiality just got sharper.** API pulls bring real client financial
data into the app automatically, in volume. The Gemini free tier may use
submitted content to improve Google's products. Before any connected client's
data flows through, move to a paid tier — this stops being a theoretical point
once Phase C is live.

**Encryption key management.** OAuth tokens for a client's accounting system
are high-value credentials. They need encrypting at rest, and the encryption
key must live outside the database — in `.env` on the VPS, never in version
control. Losing that key means re-authorising every client.
