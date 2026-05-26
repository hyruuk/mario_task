# setup_env.ps1 — first-time install on Windows 10/11.
#
# Idempotent: re-running is a no-op after a successful install.
#
# Usage (in PowerShell, from the repo root):
#   .\setup_env.ps1
#
# After it finishes:
#   .\run.ps1                    # launches the first-run config wizard
#
# Requirements assumed already present:
#   - Windows 10 21H2 or Windows 11 (with winget, included by default)
#   - Python 3.10+ on PATH (install from Microsoft Store or python.org if absent)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $RootDir ".venv"
$MarioStimuliDir = Join-Path $RootDir "data\mario.stimuli"
$MarioRom = Join-Path $MarioStimuliDir "SuperMarioBros-Nes\rom.nes"

function Log($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "[x] $msg" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------------------
# 1. Verify Python is available
# ---------------------------------------------------------------------------
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Die @"
Python is not on PATH. Install it first:
    winget install --id Python.Python.3.10
or download from https://www.python.org/downloads/.
Then re-run this script.
"@
}
$pyVer = & python --version 2>&1
Log "Found $pyVer"

# ---------------------------------------------------------------------------
# 2. Install uv
# ---------------------------------------------------------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Log "Installing uv (Python project manager)..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    # uv installer adds itself to %USERPROFILE%\.local\bin in the *new* shell;
    # add it to the current session's PATH too so the rest of this script works.
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
Log "uv $(uv --version)"

# ---------------------------------------------------------------------------
# 3. Install git-annex (required by datalad for the ROM fetch)
# ---------------------------------------------------------------------------
if (-not (Get-Command git-annex -ErrorAction SilentlyContinue)) {
    Log "Installing git-annex via winget..."
    $wingetCmd = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $wingetCmd) {
        Die @"
winget is not available. Either install it from the Microsoft Store
(search for "App Installer"), or install git-annex manually from
https://git-annex.branchable.com/install/Windows/ and re-run this script.
"@
    }
    try {
        winget install --id Joey.GitAnnex --silent --accept-package-agreements --accept-source-agreements
    } catch {
        Die @"
winget install of git-annex failed. Install it manually from
https://git-annex.branchable.com/install/Windows/ and re-run this script.
Error: $_
"@
    }
    # winget may put it in Program Files; rehash PATH for the current session.
    $env:Path = "$env:ProgramFiles\Git-Annex\bin;$env:Path"
    if (-not (Get-Command git-annex -ErrorAction SilentlyContinue)) {
        Die "git-annex was installed but is not yet on PATH. Open a new PowerShell window and re-run this script."
    }
}
Log "git-annex $(git-annex version --raw)"

# ---------------------------------------------------------------------------
# 4. Virtual environment + project install
# ---------------------------------------------------------------------------
Push-Location $RootDir
try {
    if (-not (Test-Path $VenvDir)) {
        Log "Creating virtualenv at $VenvDir"
        uv venv $VenvDir
    }
    Log "Installing mario_task and pinned deps from uv.lock (--extra dev)"
    uv sync --extra dev
} finally {
    Pop-Location
}

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Die "Expected $VenvPython after uv sync — install failed."
}

# ---------------------------------------------------------------------------
# 5. ROM data via datalad (anonymous HTTPS — no SSH key required)
# ---------------------------------------------------------------------------
if (-not (Test-Path "$MarioStimuliDir\.git")) {
    Log "Cloning mario.stimuli via datalad (anonymous HTTPS, no credentials)"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $MarioStimuliDir) | Out-Null
    & $VenvPython -m datalad install -s https://github.com/courtois-neuromod/mario.stimuli $MarioStimuliDir
} else {
    Log "mario.stimuli already cloned at $MarioStimuliDir"
}

if (-not (Test-Path $MarioRom) -or ((Get-Item $MarioRom).Length -eq 0)) {
    Log "Fetching ROM + level save-states via datalad get (public conp-ria-storage-http)"
    Push-Location $MarioStimuliDir
    try {
        & $VenvPython -m datalad get .
    } finally {
        Pop-Location
    }
}

if (-not (Test-Path $MarioRom) -or ((Get-Item $MarioRom).Length -eq 0)) {
    Die @"
rom.nes is still empty after datalad get — the remote mirror may be down.
Try manually:
    cd $MarioStimuliDir
    & $VenvPython -m datalad get .
"@
}
Log ("ROM is real: " + (Get-Item $MarioRom).Length + " bytes")

# ---------------------------------------------------------------------------
# 6. Smoke test
# ---------------------------------------------------------------------------
Log "Smoke testing imports..."
$smokeCode = @'
import importlib, sys
mods = [
    "psychopy", "psychopy.visual", "wx", "retro",
    "pandas", "sounddevice", "serial", "pylsl",
    "mario_task", "mario_task.markers", "mario_task.phases", "mario_task.settings",
]
ok = True
for m in mods:
    try:
        importlib.import_module(m)
        print(f"  ok  {m}")
    except Exception as e:
        print(f"  FAIL {m}: {e}", file=sys.stderr)
        ok = False
sys.exit(0 if ok else 1)
'@
& $VenvPython -c $smokeCode
if ($LASTEXITCODE -ne 0) { Die "Smoke test failed. See errors above." }

Log "Done. Activate the environment with:"
Write-Host "    .\.venv\Scripts\Activate.ps1"
Write-Host ""
Write-Host "Launch the experiment with:"
Write-Host "    .\run.ps1"
Write-Host "First launch will open the configuration wizard."
