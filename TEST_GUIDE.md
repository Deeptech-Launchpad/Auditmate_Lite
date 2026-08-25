# End-to-End Test Guide

Walk the whole flow with realistic data: **new customer → upload → analyse →
statements → customer review → final report**. Takes about 10 minutes.

I've already run this exact path myself and every figure ties, so you should
not hit surprises. If you do, tell me what the screen said.

---

## Before you start

```powershell
cd "c:\Auditmate lite"
.\.venv\Scripts\Activate.ps1
flask run
```

Open **http://127.0.0.1:5000** → sign in `demo@auditmate.sg` / `demo1234`

Your test files are in **`sample_data\`**:

| File | What it is |
|---|---|
| `Skyline_Trial_Balance_FY2024.xlsx` | 28-account trial balance, debits = credits = 2,105,077 |
| `Skyline_AR_Ageing_FY2024.csv` | Receivables ageing, totals 435,463 |

Both are built to tie to each other, so you can check the app's arithmetic
against known answers.

---

## Step 1 — Create the customer

**Customers → + New Customer**

| Field | Value |
|---|---|
| Customer name | `Skyline Engineering Pte. Ltd.` |
| Registered legal name | `SKYLINE ENGINEERING PTE. LTD.` |
| Entity type | Private Limited Company |
| UEN | `202198765K` |
| Contact person | `Lim Wei Sheng` |
| Email | *your own email address* — you'll send the statements here later |
| Address line 1 | `27 Woodlands Industrial Park` |
| Postal code | `757718` |
| Financial year end | 31 December |
| First financial year | `FY2024` |

**Create customer.** The engagement now appears in the **Engagement dropdown**
at the top right.

---

## Step 2 — Upload the documents

Pick the engagement from the dropdown → **1. Documents → + Upload**

Upload them **separately**, because the category applies to the whole batch:

1. Category **Trial Balance** → `Skyline_Trial_Balance_FY2024.xlsx`
2. Category **Accounts Receivable Listing** → `Skyline_AR_Ageing_FY2024.csv`

Both should show status **Queued** — nothing is read yet. That's deliberate:
you upload everything first, then analyse the batch in one go.

---

## Step 3 — Analyse

Press **⚡ Analyse 2 documents**.

Expected: *"Analysed 2 document(s) and read 35 line items."*

| Document | Rows | Flagged |
|---|---|---|
| Trial balance | 29 | 0 |
| AR ageing | 6 | 0 |

---

## Step 4 — Review & Correct

Open the trial balance → **Review**.

Check for:
- A green banner: **Trial balance check: Debits 2,105,077 · Credits 2,105,077 · balanced**
- Every account label read correctly, with its own debit or credit
- The source cell shown for each row (e.g. `Trial Balance!C7`)

Try editing a figure and tabbing away — it saves instantly, the row turns
green, and the footer totals update live. Press **Ctrl+Z**… actually there's no
undo, so just retype the original value if you change one.

Press **✓ Mark verified**. Repeat for the AR listing.

> **Worth trying:** before verifying, note that if any row were flagged amber,
> "Mark verified" would be disabled. That gate is what stops an unchecked
> figure reaching the statements.

---

## Step 5 — Generate the statements

**2. Statements → ⚡ Generate statements**

You'll get an **amber warning about unmapped accounts** — this is intentional.
The trial balance contains `Workshop consumables`, a label no rule recognises.

Open **Statement of Comprehensive Income** and check these against the expected
figures:

| Line | Should be |
|---|---|
| Revenue | 1,871,355.00 |
| Cost of sales | 1,055,446.00 |
| **Gross profit** | **815,909.00** |
| Income tax expense | 51,920.00 |

Now open **Statement of Financial Position**. You'll see a **red balance
warning** — assets 629,701 vs equity and liabilities 625,501, out by **4,200**.
That 4,200 is exactly the unmapped `Workshop consumables`. The app is correctly
refusing to pretend the accounts balance.

### Fix it — and watch it learn

Scroll to **Unmapped accounts** at the bottom of the statement. Against
`Workshop consumables`, choose **Other expenses** from the dropdown.

The page reloads and the balance check turns **green**:

| Line | Should be |
|---|---|
| Total assets | **629,701.00** |
| Accumulated profit / (loss) | 451,436.00 |
| Total equity and liabilities | **629,701.00** |
| Difference | **0.00** |

That choice is now saved as a rule for Skyline. Next year the same account maps
automatically.

Also check **Statement of Changes in Equity** and **Accounts Receivable**
(should total 435,463).

Try clicking any figure to override it — the total recalculates and the line is
badged "overridden". Clear the box to revert.

---

## Step 6 — Send to the customer

**3. Customer Review → ✉ Send for verification**

Without Gmail credentials configured you'll get an amber notice — the app still
builds everything, it just doesn't transmit. Either way you get **Version 1**.

- Download **⬇ Workbook sent** and open it. There's a "Start Here" sheet and a
  tab per statement with a **Revised Amount** column and a **Comment** column.
- Note the reference in the **Email preview** panel (e.g. `AM-2024-0005`).

### Simulate the customer replying

1. In the downloaded workbook, go to **Statement of Financial Position**
2. Find `Cash and cash equivalents` (163,754.00)
3. Type `165,000` in the **Revised Amount** column
4. Add a comment: `Bank reconciliation adjustment`
5. Save the file
6. Back in the app → **Upload customer's revised version** → choose that file

You should see: *"Saved as version 2: 1 figure(s) revised, 1 comment(s)"*, the
customer's comment displayed, and a **What changed** table showing
163,754 → 165,000.

---

## Step 7 — Finalise

Still on **Customer Review**, under *Agree the final version*:

- Version: **v2**
- Agreed by: `Lim Wei Sheng, Director`

Press **✓ Mark final & unlock report**.

> Because you changed cash without a matching entry, the balance sheet is now
> out by 1,246 — so **finalising will be blocked** with a red warning and a
> confirmation checkbox. That's the guard working. Either tick the box to
> proceed, or go back and revert the figure first.

---

## Step 8 — The audit report

It's now unlocked. **4. Audit Report** shows the builder: sections on the left,
live preview on the right.

Check the structure matches your template:

1. Cover Page and Corporate Information
2. Directors' Statement
3. Statement of Comprehensive Income
4. Statement of Financial Position
5. Statement of Changes in Equity
6. Statement of Cash Flows
7. Notes 1 – 17
8. Detailed Profit and Loss Statement

Things to try:
- **Toggle a section off** — it disappears from the preview
- **Drag to reorder** using the ⠿ handle
- **Click ✎** on a text section to edit its wording
- **Preview full report** — opens the whole document, figures filled in
- **Print / Save as PDF** — your browser's print dialog produces the PDF

> The **Independent Auditor's Report** section is off by default, because your
> template is for an audit-exempt company. Switch it on for engagements that
> aren't exempt.

---

## Starting over

```powershell
flask reset-db --yes
flask seed-demo
```

---

## What to tell me

If anything looks wrong, the useful details are: which step, what you expected,
what appeared, and anything printed in the terminal running `flask run`.
