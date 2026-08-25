# Testing Phase B — Customer Review by Secure Link

**You don't need to download anything.** Two ready-made paths:

- **Path A (5 minutes)** — the demo engagement already has a finished trial
  balance. Go straight to the customer review.
- **Path B (15 minutes)** — build a trial balance from scratch using the
  sample files in `sample_data\`, then review it.

Do **Path A first**. It tests the actual new feature.

---

## Before you start

```powershell
cd "c:\Auditmate lite"
.\.venv\Scripts\Activate.ps1
flask run
```

Sign in at <http://127.0.0.1:5000> as `demo@auditmate.sg` / `demo1234`.

Your Gmail is already configured and tested, so the emails in this guide will
really be delivered.

---

# PATH A — the fast one

## Step 1 — Point the customer's email at yourself

The demo customer has a fake address, so change it to one you can open.

**Customers → Marina Bay Trading Pte Ltd → Edit details**

Set **Email** to your own address. Save.

> This is the address the review link gets sent to, so it has to be one you
> can actually read.

## Step 2 — Look at the trial balance

Pick **Marina Bay Trading · FY2025** from the **Engagement** dropdown (top
right), then **2. Trial Balance** in the sidebar.

You should see:

- 30 accounts
- A green banner: **Balanced and fully mapped**
- Totals at the bottom: debits **924,740.00** = credits **924,740.00**
- Every account showing which statement line it maps to

This is the standard trial balance — merged from the verified documents, and
the thing the customer is about to review.

## Step 3 — Send it to the customer

**3. Customer Review** in the sidebar → right-hand panel **Send for review**.

- Link valid for: **30 days**
- Leave *"Also require a short access code"* **unticked** for now
- Press **✉ Send trial balance for review**

Two things happen:

1. The email goes to the address from Step 1 — **check your inbox**
2. A green box appears: **"Link created — copy it now"**

> **Copy that link.** It's shown once and never again, because only a hash of
> it is stored. That's deliberate — a database leak yields no working links.

## Step 4 — Be the customer

This is the part worth seeing properly. Open the link in a
**private / incognito window** (Ctrl+Shift+N in Chrome or Edge), so you're
definitely not logged in.

You should land straight on the trial balance with:

- **No login page.** No username, no password, no account to create.
- **No sidebar**, no app navigation — just this one engagement's figures
- The company name, UEN, financial year, and when the link expires

Now behave like a client:

1. Find **Cash at Bank** (76,320.00) and change the debit to `78,000`
2. In its **Your comment** column, type `Bank reconciliation adjustment`
3. Find **Trade Debtors** (118,650.00) and change the debit to `120,000`
4. Watch the footer: **"You changed"** goes to 2, and the difference stops
   being zero — the customer can see the effect of their own edits
5. Add a general note in the box at the bottom
6. Press **Submit to auditor →**

You get a thank-you page confirming 2 changes.

## Step 5 — Rule on what came back

Back in your normal (logged-in) browser: **3. Customer Review**.

You'll now see a **Round 2 — customer's changes** table with both edits:

| | |
|---|---|
| **They say** | what the customer typed |
| **We had** | what the trial balance held |
| **Change** | the difference, green up / red down |
| Their comment | shown under the account name |

Now the bit you asked for — **decide each one separately**:

- On **Cash at Bank**, press **✓ Accept**
- On **Trade Debtors**, press **✕ Reject**

Watch the right-hand **Trial balance now** panel: the difference updates after
each decision. Accepted changes are written to the trial balance; rejected
ones are recorded with your decision but leave the figures alone.

Both rows now show their status with an **undo** button, so nothing is
irreversible.

## Step 6 — Close the round and carry on

Once no changes are pending, press
**✓ Close this round and return to the trial balance**.

Then **2. Trial Balance** → confirm Cash at Bank is now 78,000 (accepted) and
Trade Debtors is back at 118,650 (rejected).

The trial balance won't balance now, because you accepted a one-sided change —
that's correct behaviour, and approval will refuse until you fix it. Either
revert the figure or tick the confirmation box.

Finally: **✓ Approve & generate statements** → the statements rebuild from the
agreed figures → **5. Audit Report** unlocks.

---

# PATH B — build a trial balance from scratch

Use this to test the whole chain including document extraction.

## Step 1 — New customer

**Customers → + New Customer**

| Field | Value |
|---|---|
| Customer name | `Skyline Engineering Pte. Ltd.` |
| Registered legal name | `SKYLINE ENGINEERING PTE. LTD.` |
| UEN | `202198765K` |
| Contact person | `Lim Wei Sheng` |
| Email | **your own address** |
| Financial year end | 31 December |
| First financial year | `FY2024` |

## Step 2 — Upload

**1. Documents → + Upload**, one category at a time:

| Category | File |
|---|---|
| Trial Balance | `sample_data\Skyline_Trial_Balance_FY2024.xlsx` |
| Accounts Receivable Listing | `sample_data\Skyline_AR_Ageing_FY2024.csv` |

Both show **Queued** — nothing is read until you say so.

## Step 3 — Analyse

Press **⚡ Analyse 2 documents**. Expect 29 rows from the trial balance and 6
from the AR listing, with **0 flagged**.

## Step 4 — Verify

Open each document → **Review** → check the figures → **✓ Mark verified**.

The trial balance document should show a green
**Debits 2,105,077 · Credits 2,105,077 · balanced** banner.

## Step 5 — Build the trial balance

**2. Trial Balance → ⚡ Build trial balance**

You'll get an amber warning: **1 account needs mapping**. That's deliberate —
the file contains `Workshop consumables`, which no rule recognises.

In its **Maps to** dropdown, choose **Other expenses**. The banner turns green
and the unmapped count drops to 0. That choice is now remembered for this
client.

## Step 6 — Continue from Path A Step 3

Send it for review, act as the customer, accept/reject, approve.

---

## Things worth deliberately trying

**Revoke a link.** Send a review, copy the link, open it (works), then press
**Revoke this link** and refresh the customer page — it shows *"This link has
been withdrawn"*.

**Send twice.** Creating a new link automatically kills the previous one. Try
the old URL: it's dead.

**Use an access code.** Tick *"Also require a short access code"* when
sending. A 4-digit code is shown to you once. Opening the link now asks for it
first. This is optional per engagement — off by default.

**Try a made-up link.** `http://127.0.0.1:5000/review/somethingmadeup` — you
get a neutral "couldn't open that link" page that reveals nothing about
whether the token was real.

---

## If something looks wrong

Tell me: which step, what you expected, what appeared, and anything printed in
the terminal running `flask run`.

## Starting over

```powershell
flask reset-db --yes
flask seed-demo
```
