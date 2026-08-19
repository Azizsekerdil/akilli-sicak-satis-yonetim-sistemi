<#
.SYNOPSIS
    Akıllı Sıcak Satış Yönetim Sistemi — one-command development setup.
    Smart Van Sales Management System — one-command development setup.

.DESCRIPTION
    Creates the Python virtual environment, installs backend and frontend
    dependencies, writes a .env file with a freshly generated secret key,
    initialises the database and (optionally) loads demo data.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup.ps1
    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WithDemoData
    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -SkipFrontend
#>
[CmdletBinding()]
param(
    [switch]$WithDemoData,
    [switch]$SkipFrontend,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$venv = Join-Path $root '.venv'
$py   = Join-Path $venv 'Scripts\python.exe'

function Write-Step($n, $text) { Write-Host "`n[$n] $text" -ForegroundColor Cyan }
function Write-Ok($text)       { Write-Host "    OK  $text" -ForegroundColor Green }
function Write-Warn2($text)    { Write-Host "    !   $text" -ForegroundColor Yellow }
function Write-Err($text)      { Write-Host "    X   $text" -ForegroundColor Red }

Write-Host "===========================================================" -ForegroundColor White
Write-Host " Akilli Sicak Satis Yonetim Sistemi - Kurulum / Setup" -ForegroundColor White
Write-Host "===========================================================" -ForegroundColor White

# ---------------------------------------------------------------------------
Write-Step 1 "Checking prerequisites"

$pythonCmd = $null
foreach ($c in @('py -3.11', 'python', 'py')) {
    try {
        $parts = $c.Split(' ')
        $v = & $parts[0] $parts[1..($parts.Length - 1)] --version 2>&1
        if ($v -match 'Python 3\.(1[1-9]|[2-9]\d)') { $pythonCmd = $c; Write-Ok "Python: $v ($c)"; break }
    } catch { }
}
if (-not $pythonCmd) {
    Write-Err "Python 3.11+ not found. Install it from https://www.python.org/downloads/"
    exit 1
}

if (-not $SkipFrontend) {
    try {
        $nodeV = node --version 2>&1
        Write-Ok "Node.js: $nodeV"
    } catch {
        Write-Warn2 "Node.js not found - frontend will be skipped. Install from https://nodejs.org/"
        $SkipFrontend = $true
    }
}

try { Write-Ok "Git: $(git --version 2>&1)" } catch { Write-Warn2 "Git not found (optional)" }

# ---------------------------------------------------------------------------
Write-Step 2 "Creating folders"
foreach ($d in @('data', 'logs', 'backups', 'reports\generated', 'data\uploads')) {
    $p = Join-Path $root $d
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Force -Path $p | Out-Null }
}
Write-Ok "data / logs / backups / reports ready"

# ---------------------------------------------------------------------------
Write-Step 3 "Python virtual environment"
if ((Test-Path $py) -and -not $Force) {
    Write-Ok ".venv already exists (use -Force to rebuild)"
} else {
    if (Test-Path $venv) { Remove-Item $venv -Recurse -Force }
    $parts = $pythonCmd.Split(' ')
    & $parts[0] $parts[1..($parts.Length - 1)] -m venv $venv
    Write-Ok ".venv created"
}

# ---------------------------------------------------------------------------
Write-Step 4 "Installing backend dependencies"
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r (Join-Path $root 'backend\requirements-dev.txt') --quiet
if ($LASTEXITCODE -ne 0) { Write-Err "pip install failed"; exit 1 }
Write-Ok "Backend packages installed"

Write-Host "    Optional: route optimisation solver (OR-Tools, ~100 MB)" -ForegroundColor DarkGray
Write-Host "      $py -m pip install ortools" -ForegroundColor DarkGray

# ---------------------------------------------------------------------------
Write-Step 5 "Environment file"
$envFile = Join-Path $root '.env'
if ((Test-Path $envFile) -and -not $Force) {
    Write-Ok ".env already exists - left untouched"
} else {
    $secret = & $py -c "import secrets;print(secrets.token_urlsafe(64))"
    $tpl = Get-Content (Join-Path $root '.env.example') -Raw -Encoding UTF8
    $tpl = $tpl -replace 'VS_SECRET_KEY=CHANGE_ME_GENERATE_A_LONG_RANDOM_STRING', "VS_SECRET_KEY=$secret"

    # Carry over an NVIDIA key that is already in the machine environment.
    if ($env:NVIDIA_API_KEY) {
        $tpl = $tpl -replace 'VS_NVIDIA_API_KEY=', "VS_NVIDIA_API_KEY=$($env:NVIDIA_API_KEY)"
        Write-Ok "NVIDIA API key picked up from the environment (not displayed)"
    }
    Set-Content -Path $envFile -Value $tpl -Encoding UTF8 -NoNewline
    Write-Ok ".env created with a freshly generated secret key"
}

# ---------------------------------------------------------------------------
Write-Step 6 "Database"
Push-Location (Join-Path $root 'backend')
try {
    & $py -m alembic upgrade head 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warn2 "alembic upgrade reported an issue - falling back to create_all" }
    & $py -c "import sys;sys.path.insert(0,'.');from app.services.bootstrap_service import ensure_baseline;r=ensure_baseline();print('    seeded:',{k:v for k,v in r.items() if not k.startswith('_')})"
    Write-Ok "Schema created and reference data seeded"
} finally {
    Pop-Location
}

# ---------------------------------------------------------------------------
if ($WithDemoData) {
    Write-Step 7 "Demo data"
    Push-Location (Join-Path $root 'backend')
    try {
        & $py -m scripts.seed_demo_data
        Write-Ok "Demo data loaded"
    } catch {
        Write-Warn2 "Demo data generation failed: $_"
    } finally {
        Pop-Location
    }
}

# ---------------------------------------------------------------------------
if (-not $SkipFrontend) {
    Write-Step 8 "Frontend"
    Push-Location (Join-Path $root 'frontend')
    try {
        npm install --no-fund --no-audit
        if ($LASTEXITCODE -ne 0) { Write-Warn2 "npm install failed" } else { Write-Ok "Frontend packages installed" }
    } finally {
        Pop-Location
    }
}

# ---------------------------------------------------------------------------
Write-Host "`n===========================================================" -ForegroundColor Green
Write-Host " Kurulum tamamlandi / Setup complete" -ForegroundColor Green
Write-Host "===========================================================" -ForegroundColor Green
Write-Host ""
Write-Host " Baslatmak icin / To start:" -ForegroundColor White
Write-Host "     .\start.bat" -ForegroundColor Yellow
Write-Host ""
Write-Host " Adresler / URLs:" -ForegroundColor White
Write-Host "     Backend API : http://127.0.0.1:8000" -ForegroundColor Gray
Write-Host "     API docs    : http://127.0.0.1:8000/docs" -ForegroundColor Gray
Write-Host "     Frontend    : http://localhost:5173" -ForegroundColor Gray
Write-Host ""
Write-Host " Ilk giris / First login: kullanici 'admin'." -ForegroundColor White
Write-Host " Sifre .env icindeki VS_ADMIN_PASSWORD ile belirlenir;" -ForegroundColor White
Write-Host " ayarlanmadiysa ilk calistirmada uretilir ve konsola yazilir." -ForegroundColor White
Write-Host ""
