# Setup Guide — Auditmate Lite

Follow these steps in order. Total time: about 15–20 minutes, most of it
waiting for installers.

> **Important:** this machine currently has **no Python and no PostgreSQL**
> installed. Nothing can run until Steps 1 and 2 are done.

---

## The fast path (one command)

```powershell
cd "c:\Auditmate lite"
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

The script does Steps 1–7 below and prompts you for the two things it can't
guess (your PostgreSQL password and your Anthropic API key).

**If Python or PostgreSQL get installed during the run**, close the terminal,
open a new one, and run the script again — Windows needs a fresh shell to pick
up the new PATH. The script is safe to re-run; it skips anything already done.

If you'd rather do it manually, or the script fails, use the steps below.

---

## Step 1 — Install Python 3.12

```powershell
winget install --id Python.Python.3.12 -e --source winget
```

Then **close and reopen your terminal** and check:

```powershell
py -3.12 --version      # should print Python 3.12.x
```

> Do not use the "Python" shortcut in the Microsoft Store — that's the stub
> currently on this machine and it isn't a real interpreter.

---

## Step 2 — Install PostgreSQL 16

```powershell
winget install --id PostgreSQL.PostgreSQL.16 -e --source winget
```

**About the password:** when winget installs PostgreSQL unattended it does *not*
prompt you — it silently sets the `postgres` superuser password to **`postgres`**.
That's what this project's `.env` uses. If you ever install PostgreSQL by
double-clicking the EDB installer instead, it *will* prompt you, and you must
put whatever you chose into `DATABASE_URL` in `.env`.

> ⚠️ `postgres`/`postgres` is fine on a local development machine, but **never
> deploy the VPS with it.** Set a strong password there and put it in the
> server's `.env`.

Close and reopen the terminal, then check:

```powershell
psql --version
```

If `psql` isn't found, add PostgreSQL to your PATH:

```powershell
$env:PATH += ";C:\Program Files\PostgreSQL\16\bin"
```

To make that permanent: Windows Search → "Edit the system environment
variables" → Environment Variables → edit `Path` → add
`C:\Program Files\PostgreSQL\16\bin`.

---

## Step 3 — Create the Python environment

```powershell
cd "c:\Auditmate lite"
py -3.12 -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

Your prompt should now start with `(.venv)`. Installing the packages takes
2–5 minutes.

---

## Step 4 — Create the database

```powershell
psql -U postgres -h localhost -c "CREATE DATABASE auditmate_dev;"
```

Enter the `postgres` password from Step 2 when prompted.

---

## Step 5 — Create the `.env` file

Copy the example and edit it:

```powershell
Copy-Item .env.example .env
notepad .env
```

Change **one line** — put your PostgreSQL password where `CHANGEME` is:

```
DATABASE_URL=postgresql+psycopg://postgres:YOUR_PASSWORD_HERE@localhost:5432/auditmate_dev
```

Leave `ANTHROPIC_API_KEY=` empty for now — Step 8 covers that.

---

## Step 6 — Create the tables

```powershell
$env:FLASK_APP = "wsgi.py"
flask init-db
```

Expected output: `Database tables created.`

---

## Step 7 — Load the demo data

```powershell
flask seed-demo
```

This creates a login and three Singapore client companies, one of which
(Marina Bay Trading Pte Ltd, FY2025) has a complete worked example: a verified
trial balance, a part-reviewed scanned document, and generated financial
statements.

Expected output:

```
Demo data loaded.
  Login:     demo@auditmate.sg
  Password:  demo1234
```

### Start the app

```powershell
flask run
```

Open **http://127.0.0.1:5000** and sign in with the demo credentials.

To stop the app, press `Ctrl+C`.

---

## Step 8 — Turn on AI extraction (optional, do this when ready)

Without a key, the app still extracts from Excel, CSV, Word and typed PDFs
using deterministic parsers. Adding a key additionally enables:

- reading **scanned PDFs and photographed documents**
- **automatic account mapping** for unfamiliar chart-of-accounts labels
- a fallback pass whenever the rule-based parser produces low-confidence output

To enable it:

1. Get a key at <https://console.anthropic.com/> → API Keys
2. Open `.env` and set:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
3. Restart the app (`Ctrl+C`, then `flask run`)

The top-right badge changes from "AI extraction off" to "✦ AI extraction on".
To re-run extraction on documents uploaded before you added the key, open the
document and click **↻ Re-extract**.

---

## Everyday commands

Every session starts with activating the environment:

```powershell
cd "c:\Auditmate lite"
.\.venv\Scripts\activate
flask run
```

| Command | What it does |
|---|---|
| `flask run` | Start the app on http://127.0.0.1:5000 |
| `flask init-db` | Create tables (safe to re-run) |
| `flask seed-demo` | Load demo customers and a worked engagement |
| `flask create-admin` | Create a real auditor login (prompts for name/email/password) |
| `flask reset-db` | **Deletes everything** and recreates empty tables |
| `python worker.py` | Background extraction worker (only if `JOBS_INLINE=0`) |

---

## Troubleshooting

**`flask: command not found`**
The virtual environment isn't active. Run `.\.venv\Scripts\activate` — your
prompt should show `(.venv)`.

**`connection to server at "localhost" failed`**
PostgreSQL isn't running. Windows Search → "Services" → find
`postgresql-x64-16` → Start. Or check your password in `.env`.

**`password authentication failed for user "postgres"`**
The password in `.env` doesn't match Step 2. Edit `DATABASE_URL` in `.env`.

**`FATAL: database "auditmate_dev" does not exist`**
Repeat Step 4.

**Uploading a scanned PDF extracts nothing**
Expected without an API key — there's no text layer to parse. Do Step 8, then
click **↻ Re-extract** on the document.

**PDF export says "PDF engine not installed"**
Expected on Windows. Use the browser's Print → Save as PDF from the preview
page. For one-click export on the Linux VPS, uncomment `WeasyPrint` in
`requirements.txt` and `pip install -r requirements.txt` there.

**Everything is broken and I want to start over**

```powershell
flask reset-db --yes
flask seed-demo
```

---

## Deploying to the Hostinger VPS (later)

1. Install Python 3.12, PostgreSQL 16, and `libpango`/`libcairo` (for WeasyPrint).
2. Copy the project, create a venv, `pip install -r requirements.txt`.
3. Uncomment `WeasyPrint` in `requirements.txt` and install it.
4. Create a `.env` with a strong `SECRET_KEY`, the production `DATABASE_URL`,
   `SESSION_COOKIE_SECURE=1`, and `JOBS_INLINE=0`.
5. Run `flask init-db` and `flask create-admin`.
6. Serve with gunicorn behind nginx:
   ```bash
   gunicorn -w 4 -b 127.0.0.1:8000 wsgi:app
   ```
7. Run `python worker.py` as a second systemd service for background extraction.
8. Set `client_max_body_size 200M;` in the nginx server block so uploads work.
