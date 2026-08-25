# Auditmate Lite - Phase 1 environment setup (Windows / PowerShell)
#
# Installs Python 3.12 and PostgreSQL 16, creates the virtual environment,
# the dev database and a starter .env file, then creates the tables and
# loads demo data. Safe to re-run - each step checks first.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# NOTE: this file is deliberately pure ASCII. Windows PowerShell 5.1 reads
# .ps1 files as ANSI unless they carry a BOM, so non-ASCII characters break
# the parser.

$ErrorActionPreference = "Stop"

function Section($title) {
    Write-Host ""
    Write-Host "=== $title ===" -ForegroundColor Cyan
}

function Test-Cmd($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

# ---------------------------------------------------------------------------
Section "1. Python 3.12"
# ---------------------------------------------------------------------------
$pythonOk = $false
try {
    $v = & py -3.12 --version 2>$null
    if ($v -match "3\.12") { $pythonOk = $true; Write-Host "Found: $v" }
} catch {}

if (-not $pythonOk) {
    Write-Host "Installing Python 3.12 via winget (may prompt for admin rights)..."
    winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
    Write-Host "Python installed. Restart this terminal for PATH changes to apply."
} else {
    Write-Host "Python 3.12 already present. Skipping."
}

# ---------------------------------------------------------------------------
Section "2. PostgreSQL 16"
# ---------------------------------------------------------------------------
if (-not (Test-Cmd "psql")) {
    Write-Host "Installing PostgreSQL 16 via winget..."
    Write-Host "IMPORTANT: the installer asks you to set a password for the"
    Write-Host "'postgres' user. Write it down - you need it further below."
    winget install --id PostgreSQL.PostgreSQL.16 -e --source winget --accept-package-agreements --accept-source-agreements
    Write-Host "PostgreSQL installed. Restart this terminal, or add its bin folder"
    Write-Host "to PATH: C:\Program Files\PostgreSQL\16\bin"
} else {
    Write-Host "PostgreSQL already present. Skipping."
}

# ---------------------------------------------------------------------------
Section "3. Python virtual environment"
# ---------------------------------------------------------------------------
$venvPath = Join-Path $PSScriptRoot ".venv"

if (-not (Test-Path $venvPath)) {
    Write-Host "Creating virtual environment at .venv ..."
    py -3.12 -m venv $venvPath
} else {
    Write-Host ".venv already exists. Skipping creation."
}

$pip = Join-Path $venvPath "Scripts\pip.exe"
$reqFile = Join-Path $PSScriptRoot "requirements.txt"

if ((Test-Path $pip) -and (Test-Path $reqFile)) {
    Write-Host "Installing dependencies (takes 2-5 minutes)..."
    & $pip install --upgrade pip
    & $pip install -r $reqFile
} else {
    Write-Host "Cannot install dependencies yet. Is Python installed and this terminal restarted?"
}

# ---------------------------------------------------------------------------
Section "4. Dev database"
# ---------------------------------------------------------------------------
$dbName = "auditmate_dev"

if (Test-Cmd "psql") {
    Write-Host "Creating database '$dbName'. You will be prompted for the"
    Write-Host "postgres password you set during installation."

    $exists = ""
    try {
        $exists = & psql -U postgres -h localhost -tAc "SELECT 1 FROM pg_database WHERE datname='$dbName'" 2>$null
    } catch {}

    if ($exists -eq "1") {
        Write-Host "Database '$dbName' already exists. Skipping."
    } else {
        & psql -U postgres -h localhost -c "CREATE DATABASE $dbName;"
        Write-Host "Created database '$dbName'."
    }
} else {
    Write-Host "psql not on PATH yet. Restart the terminal and re-run this script."
}

# ---------------------------------------------------------------------------
Section "5. .env file"
# ---------------------------------------------------------------------------
$envPath = Join-Path $PSScriptRoot ".env"

if (-not (Test-Path $envPath)) {
    $apiKey = Read-Host "Anthropic API key (press Enter to skip and add it later)"
    $secure = Read-Host "Password for the postgres user" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $pgPassword = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)

    $lines = @(
        "# Auditmate Lite - local dev environment",
        "FLASK_APP=wsgi.py",
        "FLASK_DEBUG=1",
        "SECRET_KEY=dev-only-change-me",
        "DATABASE_URL=postgresql+psycopg://postgres:$pgPassword@localhost:5432/$dbName",
        "STORAGE_ROOT=./storage",
        "JOBS_INLINE=1",
        "CONFIDENCE_THRESHOLD=0.80",
        "ANTHROPIC_API_KEY=$apiKey",
        "ANTHROPIC_MODEL=claude-opus-5"
    )

    $lines | Out-File -FilePath $envPath -Encoding utf8
    Write-Host "Wrote .env. Do NOT commit this file."
} else {
    Write-Host ".env already exists. Leaving it untouched."
}

# ---------------------------------------------------------------------------
Section "6. Create tables and load demo data"
# ---------------------------------------------------------------------------
$flask = Join-Path $venvPath "Scripts\flask.exe"

if ((Test-Path $flask) -and (Test-Path $envPath)) {
    $env:FLASK_APP = "wsgi.py"
    Write-Host "Creating database tables..."
    & $flask init-db

    Write-Host "Loading demo data..."
    & $flask seed-demo
} else {
    Write-Host "Skipping. Run these manually once the steps above succeed:"
    Write-Host "    .\.venv\Scripts\Activate.ps1"
    Write-Host "    flask init-db"
    Write-Host "    flask seed-demo"
}

# ---------------------------------------------------------------------------
Section "Done"
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Start the app with:" -ForegroundColor Green
Write-Host "    .\.venv\Scripts\Activate.ps1"
Write-Host "    flask run"
Write-Host ""
Write-Host "Then open http://127.0.0.1:5000 and sign in with:"
Write-Host "    demo@auditmate.sg  /  demo1234"
Write-Host ""
Write-Host "If Python or PostgreSQL were installed during this run, close this"
Write-Host "terminal, open a new one, and run the script again to finish."
