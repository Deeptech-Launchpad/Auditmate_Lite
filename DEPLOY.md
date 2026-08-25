# Deploying Auditmate

Two parts: get the code onto GitHub, then onto the Hostinger VPS.

Everything below assumes Ubuntu 22.04 or 24.04 on the VPS, which is what
Hostinger gives you by default.

---

## Part 1 — Push to GitHub

### 1.1 Check what will be committed

`.gitignore` already excludes the things that must never leave your machine:

| Excluded | Why |
|---|---|
| `.env` | API keys, database password, mail password |
| `storage/` | Uploaded client documents |
| `templates_private/` | The client's annual report template |
| `sample_data/*.xlsx` | Test files carrying real figures |
| `.venv/` | Rebuilt on the server anyway |

Before the first push, confirm it:

```bash
git init
git add -A
git status --short | grep -E "\.env$|storage/|templates_private/"
```

**That must print nothing.** If it prints anything, stop and fix `.gitignore`
before committing — a secret pushed to GitHub is public the moment it lands,
even in a private repo's history.

### 1.2 First commit

```bash
git config user.email "jey@deeptechskills.com"
git config user.name  "Jey"

git add -A
git commit -m "Auditmate: audit management for Singapore engagements"
```

### 1.3 Push to the repository

The repository already exists:
`https://github.com/Deeptech-Launchpad/Auditmate_Lite.git`

```bash
git remote add origin https://github.com/Deeptech-Launchpad/Auditmate_Lite.git
git branch -M main
git push -u origin main
```

If the repo was created with a README or a licence, it already has a commit
and the push is rejected as non-fast-forward. Join the two histories first:

```bash
git pull --rebase origin main
git push -u origin main
```

GitHub will ask for a password: use a **personal access token**, not your
account password. Create one at *Settings → Developer settings → Personal
access tokens → Tokens (classic)*, with the `repo` scope. If the repository
lives under the Deeptech-Launchpad organisation, the token has to be one the
organisation allows — a fine-grained token needs the organisation to approve
it before it will push.

### 1.4 Check what actually landed

Open the repository on GitHub and confirm none of these appear:
`.env`, `storage/`, `templates_private/`, `sample_data/*.xlsx`.

If one of them did, deleting it in a later commit is not enough — it stays in
the history and stays readable. The credential has to be rotated and the
history rewritten.

---

## Part 2 — Hostinger VPS

### 2.1 Connect

```bash
ssh root@YOUR_SERVER_IP
```

### 2.2 Install what the app needs

```bash
apt update && apt upgrade -y
apt install -y python3.12 python3.12-venv python3-pip \
               postgresql postgresql-contrib nginx git \
               libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0
```

The last four are WeasyPrint's dependencies. Installing them here is what
gives you **one-click PDF export** on the server — on Windows it falls back
to the browser's print dialogue, which is why it behaves differently locally.

### 2.3 Create the database

```bash
sudo -u postgres psql
```

Inside psql, with a password of your own choosing:

```sql
CREATE DATABASE auditmate;
CREATE USER auditmate WITH PASSWORD 'a-long-random-password';
GRANT ALL PRIVILEGES ON DATABASE auditmate TO auditmate;
\c auditmate
GRANT ALL ON SCHEMA public TO auditmate;
\q
```

### 2.4 Get the code

```bash
adduser --system --group --home /opt/auditmate auditmate
cd /opt/auditmate
git clone https://github.com/Deeptech-Launchpad/Auditmate_Lite.git app
cd app

python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install gunicorn weasyprint
```

### 2.5 Configure

```bash
cp .env.example .env
nano .env
```

Fill in:

```ini
FLASK_DEBUG=0
SECRET_KEY=<paste the output of the command below>
DATABASE_URL=postgresql+psycopg://auditmate_user:<password>@localhost:<port>/auditmate_db

STORAGE_ROOT=/opt/auditmate/app/storage

SMTP_USER=<the Gmail account that sends review links>
SMTP_PASSWORD=<its 16-character Gmail app password>
IMAP_HOST=imap.gmail.com

AI_PROVIDER=gemini
GEMINI_API_KEY=<your key>

# Xero stays a placeholder until the client approves the flow
XERO_CLIENT_ID=
XERO_CLIENT_SECRET=
XERO_DEMO_MODE=true
XERO_REDIRECT_URI=https://<your-domain>/integrations/xero/callback

TOKEN_ENCRYPTION_KEY=<paste the output of the second command below>
```

Generate the two keys:

```bash
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Lock the file down — it holds every credential the app has:

```bash
chmod 600 .env
chown auditmate:auditmate .env
```

### 2.6 Create the database tables and your login

```bash
.venv/bin/flask --app wsgi setup-production
```

It prompts for a password rather than taking it on the command line, so it
never lands in your shell history. This creates the schema and **one** admin
account. It does not create demo data.

Then check the configuration:

```bash
.venv/bin/flask --app wsgi check-config
.venv/bin/flask --app wsgi check-email
.venv/bin/flask --app wsgi check-ai
.venv/bin/flask --app wsgi check-xero
```

### 2.7 Run it as a service

```bash
mkdir -p /opt/auditmate/app/storage
chown -R auditmate:auditmate /opt/auditmate

nano /etc/systemd/system/auditmate.service
```

```ini
[Unit]
Description=Auditmate
After=network.target postgresql.service

[Service]
User=auditmate
Group=auditmate
WorkingDirectory=/opt/auditmate/app
Environment="PATH=/opt/auditmate/app/.venv/bin"
ExecStart=/opt/auditmate/app/.venv/bin/gunicorn \
          --workers 3 --timeout 180 --bind 127.0.0.1:8000 wsgi:app
Restart=always

[Install]
WantedBy=multi-user.target
```

`--timeout 180` matters: reading a large scanned PDF through the AI can take
longer than gunicorn's 30-second default, and the request would be killed
mid-extraction.

```bash
systemctl daemon-reload
systemctl enable --now auditmate
systemctl status auditmate
```

### 2.8 Put nginx in front

```bash
nano /etc/nginx/sites-available/auditmate
```

```nginx
server {
    listen 80;
    server_name YOUR_SERVER_IP;

    # Client workbooks and scanned PDFs are large.
    client_max_body_size 200M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 180s;
    }

    location /static/ {
        alias /opt/auditmate/app/app/static/;
        expires 30d;
    }
}
```

```bash
ln -s /etc/nginx/sites-available/auditmate /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

The app is now at `http://YOUR_SERVER_IP`.

### 2.9 HTTPS — do this before any client data goes in

Point a domain at the server, then:

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d audit.altiusnxt.com
```

Until this is done the sign-in password crosses the internet in clear text.
It also has to be done before Xero goes live: Xero will not accept a plain
`http://` redirect URI for anything but localhost.

### 2.10 Firewall

```bash
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw enable
```

Postgres is not opened — it only listens locally, which is what you want.

---

## Deploying an update

```bash
cd /opt/auditmate/app
git pull
.venv/bin/pip install -r requirements.txt
systemctl restart auditmate
```

If a release adds a database column, run `.venv/bin/flask --app wsgi init-db`
as well — it creates anything missing without touching existing data.

---

## Backups

Client accounting records are not something to lose. Two things matter: the
database and the uploaded documents.

```bash
nano /opt/auditmate/backup.sh
```

```bash
#!/bin/bash
set -e
DEST=/opt/auditmate/backups
mkdir -p "$DEST"
STAMP=$(date +%F)
sudo -u postgres pg_dump auditmate | gzip > "$DEST/db-$STAMP.sql.gz"
tar czf "$DEST/storage-$STAMP.tar.gz" -C /opt/auditmate/app storage
find "$DEST" -mtime +30 -delete
```

```bash
chmod +x /opt/auditmate/backup.sh
crontab -e
# 02:00 daily
0 2 * * * /opt/auditmate/backup.sh
```

A backup on the same server is not a backup. Copy these off the VPS —
Hostinger's own snapshots, or `rclone` to cloud storage.

---

## Before the client uses it in earnest

- [ ] **HTTPS** (2.9). Non-negotiable once real client data is involved.
- [ ] **Gemini on a paid tier.** The free tier's terms allow the provider to
      use submitted content. Client accounting records must not go through it.
- [ ] **Backups running** and verified by restoring one.
- [ ] **`SECRET_KEY` and `TOKEN_ENCRYPTION_KEY`** are real random values, not
      the defaults. `check-xero` warns if the token key is unset.
- [ ] **Change the password** from the one used during setup.

## Known limits at handover

- **Xero is a placeholder.** Demo mode; no real connection has ever been made.
- **Alembic is not initialised.** Schema changes are applied by `init-db`,
  which adds but never alters or drops.
- **No automated test suite.** Verification has been end-to-end by hand.
