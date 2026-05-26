# mario_task — Windows installer (PowerShell).
#
# Called by install.bat. Installs the prerequisites a Windows machine
# typically lacks (Python, Git, uv), then hands off to setup_env.ps1
# which does the actual venv + datalad + ROM fetch.
#
# After a successful run there's a "Run Mario Task" shortcut on the
# desktop; the operator double-clicks it to launch sessions.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Script lives at the root of the extracted ZIP; that's also where we install.
$InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DesktopShortcut = Join-Path $env:USERPROFILE "Desktop\Run Mario Task.lnk"

function Log($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "[x] $msg" -ForegroundColor Red; exit 1 }

function Has-Command($name) {
    return [bool] (Get-Command $name -ErrorAction SilentlyContinue)
}

# Rehash PATH from system + user registries. After winget installs a tool
# the binary lives under a new directory; we need PATH updated for THIS
# session so subsequent steps can find it (a fresh shell would pick it
# up automatically, but we're already running).
function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

# ---------------------------------------------------------------------------
# 0. winget availability (gate everything else on this)
# ---------------------------------------------------------------------------
if (-not (Has-Command winget)) {
    Die @"
winget (Windows Package Manager) is not available on this computer.

  - On Windows 11: it should be built-in. Try restarting and re-running.
  - On Windows 10: open the Microsoft Store, search for "App Installer",
    install it (free), then re-run install.bat.

Direct link to App Installer:
  https://www.microsoft.com/store/productId/9NBLGGH4NNS1
"@
}

# ---------------------------------------------------------------------------
# 1. Python 3.10+
# ---------------------------------------------------------------------------
if (-not (Has-Command python)) {
    Log "Installing Python 3.10 (via winget)..."
    winget install --id Python.Python.3.10 --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Die "Python install via winget failed (exit $LASTEXITCODE). Install manually from https://www.python.org/downloads/ and re-run."
    }
    Refresh-Path
}
$pythonVer = & python --version 2>&1
Log "Python: $pythonVer"

# ---------------------------------------------------------------------------
# 2. Git (required by datalad / git-annex)
# ---------------------------------------------------------------------------
if (-not (Has-Command git)) {
    Log "Installing Git (via winget)..."
    winget install --id Git.Git --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Die "Git install via winget failed (exit $LASTEXITCODE). Install manually from https://git-scm.com/download/win and re-run."
    }
    Refresh-Path
}
Log "Git: $(git --version 2>&1)"

# ---------------------------------------------------------------------------
# 3. uv (Python project + venv manager)
# ---------------------------------------------------------------------------
if (-not (Has-Command uv)) {
    Log "Installing uv..."
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Die "uv install failed: $_. Install manually from https://docs.astral.sh/uv/getting-started/installation/ and re-run."
    }
    # uv's installer adds itself to %USERPROFILE%\.local\bin in the *next*
    # shell; nudge the current one so subsequent steps see it.
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
Log "uv: $(uv --version 2>&1)"

# ---------------------------------------------------------------------------
# 4. Run setup_env.ps1 — installs git-annex via winget, builds the venv,
#    fetches the ROM via datalad, runs the smoke test.
# ---------------------------------------------------------------------------
Log "Running setup_env.ps1 (this is the long step)..."
$setupScript = Join-Path $InstallDir "setup_env.ps1"
if (-not (Test-Path $setupScript)) {
    Die "setup_env.ps1 not found at $setupScript. The ZIP may be incomplete; re-download from GitHub Releases."
}
Set-Location $InstallDir
& $setupScript
if ($LASTEXITCODE -ne 0) {
    Die "setup_env.ps1 failed (exit $LASTEXITCODE). See the output above for the failing step."
}

# ---------------------------------------------------------------------------
# 5. Desktop shortcut
# ---------------------------------------------------------------------------
Log "Creating desktop shortcut 'Run Mario Task'..."
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($DesktopShortcut)
$Shortcut.TargetPath = "powershell.exe"
$Shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$InstallDir\run.ps1`""
$Shortcut.WorkingDirectory = $InstallDir
$Shortcut.IconLocation = "shell32.dll,21"
$Shortcut.Description = "Launch a mario_task session"
$Shortcut.Save()

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "  Double-click 'Run Mario Task' on your desktop to begin."  -ForegroundColor Green
Write-Host "  First launch will open the configuration wizard."          -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
