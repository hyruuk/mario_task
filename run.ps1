# run.ps1 -- launch the Mario experiment on Windows.
#
# Behaviour mirrors run.sh on Linux.
#
# Usage:
#   .\run.ps1                      # interactive (wizard / subject picker)
#   .\run.ps1 sub01 01             # skip the subject picker

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $RootDir ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Error "Virtualenv not found at $VenvPython. Run .\setup_env.ps1 first."
    exit 1
}

Push-Location $RootDir
try {
    & $VenvPython -m mario_task @args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
