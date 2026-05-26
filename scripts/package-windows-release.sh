#!/usr/bin/env bash
# Build a Windows-deploy ZIP from the current git checkout.
#
# Usage:
#   bash scripts/package-windows-release.sh
#
# Output:
#   dist/mario_task-windows-vX.Y.Z.zip
#
# What's in the ZIP:
#   - install.bat + install.ps1 (operator double-click entry)
#   - setup_env.ps1, run.ps1
#   - the mario_task/ Python package, tests/, pyproject.toml, uv.lock
#   - README.md, README-WINDOWS.txt, docs/, justfile, .env.example
#
# What's NOT in the ZIP:
#   - .git/, .venv/, output/, data/mario.stimuli/, config.json
#   - __pycache__/, .pytest_cache/, .ruff_cache/, dist/
#   - the Linux-only setup_env.sh and run.sh (operator runs install.bat instead)
#
# We use `git ls-files` so anything not committed (local cruft, stale
# files) never makes it into a release ZIP — committing is the gate.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"

cd "${ROOT_DIR}"

# Extract version from pyproject.toml so the ZIP name carries it.
VERSION="$(
    grep -E '^version\s*=' pyproject.toml \
        | head -1 \
        | sed -E 's/^version\s*=\s*"([^"]+)".*$/\1/'
)"
if [[ -z "${VERSION}" ]]; then
    echo "Could not extract version from pyproject.toml" >&2
    exit 1
fi

ZIP_NAME="mario_task-windows-v${VERSION}.zip"
ZIP_PATH="${DIST_DIR}/${ZIP_NAME}"

# Guard against running outside a git checkout (a release tag build via
# `git archive` would also work, but git ls-files needs a real repo).
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Not a git checkout; package-windows-release.sh requires git." >&2
    exit 1
fi

# Refuse to package dirty trees — releases should reflect committed state.
# Override with PACKAGE_ALLOW_DIRTY=1 for local testing.
if [[ -z "${PACKAGE_ALLOW_DIRTY:-}" ]] && ! git diff-index --quiet HEAD --; then
    echo "Working tree has uncommitted changes; refusing to package." >&2
    echo "  Commit / stash, or set PACKAGE_ALLOW_DIRTY=1 to override." >&2
    exit 1
fi

mkdir -p "${DIST_DIR}"
rm -f "${ZIP_PATH}"

# File list: every tracked file except those for Linux-only or per-deployment state.
TMP_LIST="$(mktemp)"
trap 'rm -f "${TMP_LIST}"' EXIT

# `--cached` = tracked, `--others --exclude-standard` = untracked but not
# in .gitignore. The union lets local dev test the script before
# committing new files (like install.bat on its first day). In CI the
# checkout is clean so `--others` is empty and behaviour is identical.
git ls-files --cached --others --exclude-standard \
    | grep -vE '^(setup_env\.sh|run\.sh)$' \
    | grep -vE '^data/' \
    | grep -vE '^output/' \
    | grep -vE '^dist/' \
    | grep -vE '^\.local-libs/' \
    | grep -vE '^config\.json$' \
    | sort > "${TMP_LIST}"

FILE_COUNT="$(wc -l < "${TMP_LIST}")"
if (( FILE_COUNT == 0 )); then
    echo "No files matched after filtering; check the grep filters." >&2
    exit 1
fi

echo "Building ${ZIP_NAME} (${FILE_COUNT} files)..."
zip --quiet "${ZIP_PATH}" -@ < "${TMP_LIST}"

echo "Done: ${ZIP_PATH}"
echo "Size: $(du -h "${ZIP_PATH}" | cut -f1)"
echo ""
echo "Contents preview (top-level files only):"
unzip -l "${ZIP_PATH}" | awk 'NR>3 {print $4}' | awk -F/ '!seen[$1]++' | head -20
