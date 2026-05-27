# setup_env.ps1 -- first-time install on Windows 10/11.
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

# After a winget install the new binary lives at a location not in this
# session's PATH; refresh from the system/user registry so the rest of
# the script can find it.
function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

# ---------------------------------------------------------------------------
# 0. winget availability gate
# ---------------------------------------------------------------------------
# We rely on winget for git-annex; surface a clean error early rather
# than failing deep inside an install step.
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Die @"
winget (Windows Package Manager) is not available on this machine.
  - On Windows 11: it should be built-in. Try restarting and re-running.
  - On Windows 10: open the Microsoft Store, search for "App Installer",
    install it (free), then re-run this script.
Direct link: https://www.microsoft.com/store/productId/9NBLGGH4NNS1
"@
}

# ---------------------------------------------------------------------------
# 1. Verify Python is available (and actually works, not just the
#    Microsoft Store "App Installer" stub that Win 11 ships).
# ---------------------------------------------------------------------------
function Test-Real-Python {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) { return $false }
    $out = & python -c "import sys; sys.stdout.write(str(sys.version_info[0]))" 2>&1
    if ($LASTEXITCODE -ne 0) { return $false }
    if ($out -match "Microsoft Store|not found|disabled") { return $false }
    try { return ([int]$out -ge 3) } catch { return $false }
}
if (-not (Test-Real-Python)) {
    Die @"
Python is not actually working on PATH. (Note: Windows 11 ships a
"Microsoft Store stub" at python.exe that LOOKS like Python but isn't.)
Install real Python first:
    winget install --id Python.Python.3.10 --scope user
or download from https://www.python.org/downloads/ (tick "Add Python to PATH").
Then open a NEW PowerShell window and re-run this script.
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
# 2b. Microsoft Visual C++ Build Tools (required to build stable-retro
#     from source -- there's no Windows wheel on PyPI).
# ---------------------------------------------------------------------------
function Test-MSVC {
    # We need vcvars64.bat reachable -- that's the script stable-retro's
    # setup.py invokes (via setuptools' msvc helper) to set up the C++
    # compiler env. Querying vswhere with `-property installationPath`
    # gives us a single path string per matching VS instance.
    $vswhere = Join-Path "${env:ProgramFiles(x86)}" "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path $vswhere)) { return $false }
    $paths = & $vswhere -latest -prerelease -products * `
        -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
        -property installationPath 2>$null
    if (-not $paths) { return $false }
    # vswhere can emit multiple lines for multiple instances; take the first.
    $first = ($paths | Select-Object -First 1)
    if ([string]::IsNullOrWhiteSpace($first)) { return $false }
    return (Test-Path (Join-Path $first.Trim() "VC\Auxiliary\Build\vcvars64.bat"))
}

if (-not (Test-MSVC)) {
    Log "Installing MSVC Build Tools 2022 (large download, ~3-4 GB, ~5-10 min)..."
    # The --override flag passes args to the underlying VS installer.
    # VCTools workload = the C++ build tools; SDK + ATL/MFC are pulled
    # in as transitive deps. --quiet --wait --nocache makes it batchable.
    winget install --id Microsoft.VisualStudio.2022.BuildTools --source winget --silent `
        --accept-package-agreements --accept-source-agreements `
        --override "--quiet --wait --nocache --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 --add Microsoft.VisualStudio.Component.Windows11SDK.22621"
    if ($LASTEXITCODE -ne 0) {
        Die @"
MSVC Build Tools install failed (exit $LASTEXITCODE).

Install manually from https://aka.ms/vs/17/release/vs_BuildTools.exe
(tick "Desktop development with C++"), then re-run this script.
"@
    }
    Refresh-Path
    # Trust winget's exit code; don't re-gate on Test-MSVC, which can
    # lag behind the installer (the VS Installer registry write may not
    # flush before this script continues). If MSVC is genuinely missing,
    # uv sync will surface a "cl.exe not found" error downstream.
}
# Helpful diagnostic so we can see which VS instance was picked.
$vswhereExe = Join-Path "${env:ProgramFiles(x86)}" "Microsoft Visual Studio\Installer\vswhere.exe"
if (Test-Path $vswhereExe) {
    $vsInst = & $vswhereExe -latest -prerelease -products * `
        -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
        -property installationPath 2>$null | Select-Object -First 1
    if ($vsInst) { Log "MSVC Build Tools at: $vsInst" }
    else { Warn "No VS instance with VC.Tools.x86.x64 visible to vswhere; continuing anyway." }
} else {
    Warn "vswhere.exe not found; cannot verify MSVC. Continuing anyway."
}

# CMake is needed by stable-retro's setup.py; not bundled with VS BuildTools' VCTools workload.
if (-not (Get-Command cmake -ErrorAction SilentlyContinue)) {
    Log "Installing CMake..."
    winget install --id Kitware.CMake --source winget --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Die "CMake install failed (exit $LASTEXITCODE). Install manually from https://cmake.org/download/ and re-run."
    }
    Refresh-Path
}
Log "CMake $((& cmake --version | Select-Object -First 1) -replace 'cmake version ','')"

# ---------------------------------------------------------------------------
# 2d. Chocolatey bootstrap.
# ---------------------------------------------------------------------------
# We use choco for several Windows tooling installs below (git-annex,
# GNU make, MinGW). On GitHub Actions runners choco is preinstalled.
# On a fresh operator box it usually isn't, but the official install
# script is a small one-liner.
if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
    Log "Installing Chocolatey package manager..."
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    try {
        Invoke-Expression ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
    } catch {
        Die @"
Chocolatey install failed: $_
Install manually following https://chocolatey.org/install , then re-run this script.
"@
    }
    Refresh-Path
    if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
        Die "choco still not on PATH after install. Open a new PowerShell window and re-run this script."
    }
}
Log "choco $(choco --version)"

# ---------------------------------------------------------------------------
# 2e. GNU make + MinGW.
# ---------------------------------------------------------------------------
# stable-retro's setup.py hardcodes `cmake -G "Unix Makefiles"` followed
# by `subprocess.check_call(["make", "-j"])`. On Windows that requires:
#   - make.exe -- GNU make (the choco `make` package).
#   - gcc/g++  -- a Unix-style toolchain. choco `mingw` provides MinGW-w64.
#   - zlib     -- via cmake's find_package(ZLIB). MinGW ships libz with
#                 its sysroot, so installing mingw also satisfies this.
if (-not (Get-Command make -ErrorAction SilentlyContinue)) {
    Log "Installing GNU make (needed by stable-retro)..."
    choco install make -y --no-progress --limit-output
    if ($LASTEXITCODE -ne 0) { Die "choco install make failed (exit $LASTEXITCODE)." }
    Refresh-Path
}
Log "make $((& make --version | Select-Object -First 1))"

# Pin MinGW to 13.2.0 (GCC 13). MinGW 15.x (GCC 15) enforces stricter
# C prototype semantics: `void f();` now means `void f(void)` rather
# than "unspecified arguments", which makes stable-retro's NES core
# fail to compile (src/fds.c declares FDSSound() then defines
# FDSSound(int c)). The windows-latest runner preinstalls MinGW 15
# at C:\mingw64, so we install 13.2.0 via choco AND prepend its bin
# directory to PATH so it wins over the system one.
$currentGccMajor = $null
if (Get-Command gcc -ErrorAction SilentlyContinue) {
    $currentGccMajor = (& gcc -dumpversion 2>&1) -split '\.' | Select-Object -First 1
}
if ($currentGccMajor -ne "13") {
    Log "Installing MinGW-w64 13.2.0 via choco (current gcc major: '$currentGccMajor')..."
    choco install mingw --version=13.2.0 -y --no-progress --limit-output --force
    if ($LASTEXITCODE -ne 0) { Die "choco install mingw 13.2.0 failed (exit $LASTEXITCODE)." }
    Refresh-Path
    # The choco mingw package puts binaries under one of a few different
    # locations depending on version. Check the common ones.
    $candidates = @(
        "C:\ProgramData\mingw64\mingw64\bin",
        "C:\ProgramData\chocolatey\lib\mingw\tools\install\mingw64\bin",
        "C:\tools\mingw64\bin"
    )
    $found = $candidates | Where-Object { Test-Path (Join-Path $_ "gcc.exe") } | Select-Object -First 1
    if (-not $found) {
        # Last-ditch deep search.
        $hit = Get-ChildItem -Path "C:\ProgramData", "C:\tools" -Filter "gcc.exe" -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.DirectoryName -match "13(\.2)?(\.0)?" } |
            Select-Object -First 1
        if ($hit) { $found = $hit.DirectoryName }
    }
    if (-not $found) {
        Die "choco mingw 13.2.0 installed but gcc.exe not found under any expected path."
    }
    # Remove the system MinGW from PATH so it can't sneak into the
    # build subprocess. windows-latest preinstalls MinGW 15 at
    # C:\mingw64; v0.2.13's logs showed PATH-prepend wasn't enough --
    # the actual compile ended up using C:/mingw64/lib/gcc/.../15.2.0
    # headers anyway. Strip it.
    $env:Path = ($env:Path -split ';' | Where-Object {
        $_ -ne "" -and $_ -notlike "C:\mingw64*"
    }) -join ';'
    $env:Path = "$found;$env:Path"
    Log "Prepended $found to PATH (and stripped C:\mingw64*)."
    # Sanity: confirm gcc on PATH is now 13.x.
    $newMajor = (& gcc -dumpversion 2>&1) -split '\.' | Select-Object -First 1
    if ($newMajor -ne "13") {
        Die "After prepending $found, gcc major is still '$newMajor' (wanted 13). System PATH still wins; check install."
    }
    # Belt-and-suspenders: pin CC/CXX so cmake and any other build
    # tooling latches onto these specific binaries regardless of PATH.
    $env:CC  = (Join-Path $found "gcc.exe")
    $env:CXX = (Join-Path $found "g++.exe")
}
Log "gcc $((& gcc --version | Select-Object -First 1))"

# ---------------------------------------------------------------------------
# 2f. zlib via vcpkg for stable-retro.
# ---------------------------------------------------------------------------
# stable-retro's CMakeLists.txt:87 does find_package(ZLIB). The choco
# MinGW distribution doesn't actually ship libz in a path cmake's
# FindZLIB module locates, so we install zlib explicitly via vcpkg and
# tell cmake about it through STABLE_RETRO_CMAKE_ARGS later.
$VcpkgRoot = if (Test-Path "C:\vcpkg\vcpkg.exe") {
    "C:\vcpkg"  # preinstalled on GitHub Actions windows-latest
} else {
    Join-Path $RootDir ".vcpkg"
}

if (-not (Test-Path (Join-Path $VcpkgRoot "vcpkg.exe"))) {
    if (-not (Test-Path $VcpkgRoot)) {
        Log "Cloning vcpkg into $VcpkgRoot ..."
        git clone --depth 1 https://github.com/microsoft/vcpkg $VcpkgRoot
        if ($LASTEXITCODE -ne 0) { Die "git clone vcpkg failed (exit $LASTEXITCODE)." }
    }
    Log "Bootstrapping vcpkg..."
    & (Join-Path $VcpkgRoot "bootstrap-vcpkg.bat") -disableMetrics
    if ($LASTEXITCODE -ne 0) { Die "vcpkg bootstrap failed (exit $LASTEXITCODE)." }
}
Log "vcpkg at $VcpkgRoot"

# Idempotent: vcpkg install is a no-op if the package is already there.
Log "Installing zlib (x64-mingw-dynamic) via vcpkg..."
& (Join-Path $VcpkgRoot "vcpkg.exe") install "zlib:x64-mingw-dynamic" --clean-after-build
if ($LASTEXITCODE -ne 0) { Die "vcpkg install zlib failed (exit $LASTEXITCODE)." }

# Forward slashes avoid quoting headaches when this gets shlex-split
# by stable-retro's setup.py.
$VcpkgRootSlash = $VcpkgRoot -replace '\\', '/'
$ToolchainFile  = "$VcpkgRootSlash/scripts/buildsystems/vcpkg.cmake"

# ---------------------------------------------------------------------------
# 3. Install git-annex (required by datalad for the ROM fetch)
# ---------------------------------------------------------------------------
# Strategy: prefer Chocolatey when available. The kitenet Inno installer
# hangs in non-interactive sessions (GitHub Actions runners, automation),
# because the silent flags don't suppress the elevation prompt -- which
# never resolves when no human is at the keyboard. Choco wraps the same
# Inno installer with its own elevation handling and runs to completion.
# On operator boxes without choco we fall back to running the Inno
# installer directly; the operator sees and clicks through the UAC
# prompt themselves.
if (-not (Get-Command git-annex -ErrorAction SilentlyContinue)) {
    if (Get-Command choco -ErrorAction SilentlyContinue) {
        Log "Installing git-annex via Chocolatey..."
        # --ignore-checksums: the choco package version often lags
        # behind kitenet's actual published installer (the same .exe
        # gets updated without a choco version bump), so the package's
        # baked-in SHA256 fails to match. We're downloading from the
        # official git-annex distribution mirror either way, so this
        # isn't a meaningful security relaxation.
        choco install git-annex -y --no-progress --limit-output --ignore-checksums
        if ($LASTEXITCODE -ne 0) {
            Die @"
choco install git-annex failed (exit $LASTEXITCODE).
Install manually from https://git-annex.branchable.com/install/Windows/
then re-run this script.
"@
        }
        Refresh-Path
    } else {
        $gitAnnexUrl = "https://downloads.kitenet.net/git-annex/windows/current/git-annex-installer.exe"
        $installer = Join-Path $env:TEMP "git-annex-installer.exe"
        Log "Downloading git-annex installer from $gitAnnexUrl ..."
        try {
            Invoke-WebRequest -Uri $gitAnnexUrl -OutFile $installer -UseBasicParsing
        } catch {
            Die @"
Could not download git-annex installer from $gitAnnexUrl
($_)
Install it manually from https://git-annex.branchable.com/install/Windows/
then re-run this script.
"@
        }
        Log "Running the git-annex installer..."
        Log "  A User Account Control prompt will appear -- click 'Yes' to allow it."
        # Standard Inno Setup silent flags: VERYSILENT skips all UI;
        # SUPPRESSMSGBOXES kills the few prompts /VERYSILENT misses;
        # SP- skips the "this will install" preamble; NORESTART = no reboot.
        # NOTE: do not invoke this branch from a non-interactive session
        # (CI, scheduled task, etc.) -- the UAC prompt has no way to resolve.
        $process = Start-Process -FilePath $installer `
            -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/SP-","/NORESTART" `
            -Wait -PassThru
        Remove-Item $installer -Force -ErrorAction SilentlyContinue
        if ($process.ExitCode -ne 0) {
            Die "git-annex installer exited with $($process.ExitCode)."
        }
        Refresh-Path
    }
    # The installer drops binaries under one of two locations depending
    # on whether it installed system-wide or per-user. Add both as
    # fallbacks in case the registry hasn't reflected the change yet.
    foreach ($p in @(
        "$env:ProgramFiles\Git-Annex\bin",
        "$env:LOCALAPPDATA\Programs\Git-Annex\bin"
    )) {
        if (Test-Path (Join-Path $p "git-annex.exe")) {
            $env:Path = "$p;$env:Path"
        }
    }
    if (-not (Get-Command git-annex -ErrorAction SilentlyContinue)) {
        Die "git-annex installed but not on PATH. Open a new PowerShell window and re-run this script."
    }
}
Log "git-annex $(git-annex version --raw)"

# ---------------------------------------------------------------------------
# 3b. Vendor stable-retro into .cache/stable-retro and patch.
# ---------------------------------------------------------------------------
# pyproject.toml declares stable-retro as a path-source pointing here.
# We need the runtime patch (skip fbneo / parallel_n64 on Windows --
# the upstream build is broken on Windows; we only need NES anyway).
$StableRetroDir = Join-Path $RootDir ".cache\stable-retro"
$StableRetroRev = "24180f5dcc4ec3ba725f9614823c22ef5c6983ff"
$StableRetroRepo = "https://github.com/Farama-Foundation/stable-retro"
if (-not (Test-Path (Join-Path $StableRetroDir ".git"))) {
    Log "Cloning stable-retro into $StableRetroDir (large; ~600 MB with submodules)..."
    New-Item -ItemType Directory -Force -Path (Split-Path $StableRetroDir) | Out-Null
    if (Test-Path $StableRetroDir) { Remove-Item -Recurse -Force $StableRetroDir }
    git clone $StableRetroRepo $StableRetroDir
    if ($LASTEXITCODE -ne 0) { Die "git clone stable-retro failed (exit $LASTEXITCODE)." }
}
Push-Location $StableRetroDir
try {
    git fetch --depth 1 origin $StableRetroRev 2>$null  # may fail if rev already present; ignore
    git checkout $StableRetroRev
    if ($LASTEXITCODE -ne 0) { Die "git checkout $StableRetroRev failed (exit $LASTEXITCODE)." }
    git submodule update --init --recursive --depth 1
    if ($LASTEXITCODE -ne 0) { Die "git submodule update failed (exit $LASTEXITCODE)." }
} finally {
    Pop-Location
}

# Patch every stable-retro src/*.h that uses fixed-width int types
# without including <cstdint>. On Linux a transitive header pulls
# <cstdint> in; on Windows/MinGW it doesn't, so the file fails with
# "int64_t does not name a type". Idempotent via the marker.
$headersNeedingCstdint = @(
    "src\data.h", "src\memory.h", "src\movie-bk2.h",
    "src\movie.h", "src\search.h", "src\utils.h"
)
foreach ($rel in $headersNeedingCstdint) {
    $headerFile = Join-Path $StableRetroDir $rel
    if (-not (Test-Path $headerFile)) { continue }
    $hcontent = Get-Content $headerFile -Raw
    if ($hcontent -match "mario_task-patch: cstdint") { continue }
    Log "Patching $rel to include <cstdint>..."
    # Inject after the #pragma once (every header has one) -- avoids
    # depending on a specific #include line being present.
    $headerReplacement = '$1' + "`r`n#include <cstdint> // mario_task-patch: cstdint -- fixed-width int types used below"
    $patched = $hcontent -replace '(?m)^(#pragma once)', $headerReplacement
    Set-Content -NoNewline -Path $headerFile -Value $patched
    if ((Get-Content $headerFile -Raw) -notmatch "mario_task-patch: cstdint") {
        Die "$rel cstdint patch did not stick. Inspect $headerFile."
    }
}

# Apply the fbneo skip patch. Marker prevents re-applying.
# Use a regex so CRLF (Windows) and LF (Linux) checkouts both match.
$cmakeFile = Join-Path $StableRetroDir "CMakeLists.txt"
$content = Get-Content $cmakeFile -Raw
if ($content -notmatch "mario_task-patch: skip fbneo") {
    Log "Patching stable-retro/CMakeLists.txt to skip fbneo + parallel_n64 on Windows..."
    # The upstream block is:
    #   if(APPLE)
    #     message(
    #       WARNING
    #         "FBNeo arcade and parallel N64 emulator is currently not supported on macOS"
    #     )
    #   else()
    #     add_core(fbneo fbneo)
    # Widening the condition makes the WIN32 path take the skip branch.
    $pattern = '(?ms)^if\(APPLE\)\s*\r?\n\s*message\(\s*\r?\n\s*WARNING\s*\r?\n\s*"FBNeo arcade and parallel N64 emulator is currently not supported on macOS"\s*\r?\n\s*\)\s*\r?\n\s*else\(\)\s*\r?\n\s*add_core\(fbneo fbneo\)'
    $replacement = "if(APPLE OR WIN32) # mario_task-patch: skip fbneo + parallel_n64 on Windows`r`n  message(`r`n    WARNING`r`n      `"FBNeo arcade and parallel N64 emulator is not supported on this platform (mario_task-patch)`"`r`n  )`r`nelse()`r`n  add_core(fbneo fbneo)"
    if ($content -notmatch $pattern) {
        Die "Could not locate the fbneo skip patch target in stable-retro CMakeLists.txt; upstream layout may have changed."
    }
    $content = [regex]::Replace($content, $pattern, $replacement)
    Set-Content -NoNewline -Path $cmakeFile -Value $content
    if ((Get-Content $cmakeFile -Raw) -notmatch "mario_task-patch") {
        Die "Patch applied but marker not visible afterwards. Inspect $cmakeFile."
    }
}
Log "stable-retro at $StableRetroDir (commit $StableRetroRev, patched)."

# ---------------------------------------------------------------------------
# 4. Virtual environment + project install
# ---------------------------------------------------------------------------
Push-Location $RootDir
try {
    if (-not (Test-Path $VenvDir)) {
        Log "Creating virtualenv at $VenvDir"
        uv venv $VenvDir
        if ($LASTEXITCODE -ne 0) { Die "uv venv failed (exit $LASTEXITCODE)." }
    }
    Log "Installing mario_task and pinned deps from uv.lock (--extra dev)"
    Log "  (first run takes ~3 min; stable-retro builds from source on Windows)"
    # stable-retro's setup.py hardcodes `cmake -G "Unix Makefiles"` but
    # appends our extra args after, and cmake honors the LAST -G in the
    # arg list. Overriding to "MinGW Makefiles" makes cmake configure
    # its toolchain against the MinGW gcc we installed above.
    # Pointing at the vcpkg toolchain file makes find_package(ZLIB)
    # locate the zlib we installed in stage 2f.
    $stableRetroExtra = "-G `"MinGW Makefiles`" -DCMAKE_TOOLCHAIN_FILE=$ToolchainFile -DVCPKG_TARGET_TRIPLET=x64-mingw-dynamic"
    if ($env:CC)  { $stableRetroExtra += " -DCMAKE_C_COMPILER=$($env:CC   -replace '\\', '/')" }
    if ($env:CXX) { $stableRetroExtra += " -DCMAKE_CXX_COMPILER=$($env:CXX -replace '\\', '/')" }
    # stable-retro's setup.py passes -DPython_LIBRARY=<platlib-dir> which
    # is the site-packages directory, not a libpython file -- so cmake
    # ends up putting an empty value into PYBIND_LIBS and the final link
    # fails with "undefined reference to __imp__Py_NoneStruct" etc.
    # Override with the real python<ver>.lib at the Python install root.
    $pyVer = & python -c "import sys; print(f'{sys.version_info.major}{sys.version_info.minor}')"
    $pyPrefix = & python -c "import sys; print(sys.base_prefix)"
    $pyLib = Join-Path $pyPrefix "libs\python$pyVer.lib"
    if (Test-Path $pyLib) {
        $stableRetroExtra += " -DPython_LIBRARY=$($pyLib -replace '\\', '/')"
        Log "Pinning Python_LIBRARY to $pyLib"
    } else {
        Warn "Could not find $pyLib; stable-retro link may fail."
    }
    $env:STABLE_RETRO_CMAKE_ARGS = $stableRetroExtra
    uv sync --extra dev
    if ($LASTEXITCODE -ne 0) {
        Die @"
uv sync failed (exit $LASTEXITCODE).

A common cause on Windows is missing the C++ compiler that stable-retro
needs to build from source. Install MSVC Build Tools:

  winget install --id Microsoft.VisualStudio.2022.BuildTools --source winget --override "--quiet --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.Windows11SDK"

(this is ~3-4 GB and takes a while). Then re-run install.bat.
"@
    }
} finally {
    Pop-Location
}

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Die "Expected $VenvPython after uv sync -- install failed."
}
$VenvDatalad = Join-Path $VenvDir "Scripts\datalad.exe"
if (-not (Test-Path $VenvDatalad)) {
    Die @"
datalad.exe not at $VenvDatalad after uv sync.
This usually means uv sync didn't actually finish installing all
packages (maybe stable-retro's build silently dropped datalad). Try:
  & '$VenvPython' -m pip install datalad
to install it directly, then re-run this script.
"@
}

# ---------------------------------------------------------------------------
# 5. ROM data via datalad (anonymous HTTPS -- no SSH key required)
# ---------------------------------------------------------------------------
if (-not (Test-Path "$MarioStimuliDir\.git")) {
    Log "Cloning mario.stimuli via datalad (anonymous HTTPS, no credentials)"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $MarioStimuliDir) | Out-Null
    & (Join-Path $VenvDir "Scripts\datalad.exe") install -s https://github.com/courtois-neuromod/mario.stimuli $MarioStimuliDir
} else {
    Log "mario.stimuli already cloned at $MarioStimuliDir"
}

if (-not (Test-Path $MarioRom) -or ((Get-Item $MarioRom).Length -eq 0)) {
    Log "Fetching ROM + level save-states via datalad get (public conp-ria-storage-http)"
    Push-Location $MarioStimuliDir
    try {
        & (Join-Path $VenvDir "Scripts\datalad.exe") get .
    } finally {
        Pop-Location
    }
}

if (-not (Test-Path $MarioRom) -or ((Get-Item $MarioRom).Length -eq 0)) {
    Die @"
rom.nes is still empty after datalad get -- the remote mirror may be down.
Try manually:
    cd $MarioStimuliDir
    & (Join-Path $VenvDir "Scripts\datalad.exe") get .
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
