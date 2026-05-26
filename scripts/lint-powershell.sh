#!/usr/bin/env bash
# Lint every .ps1 file in the repo using PowerShell's built-in parser.
#
# Catches: syntax errors, unclosed braces, non-ASCII characters that
# break Windows PowerShell 5.x's tokenizer.
#
# Requires `pwsh` on PATH. On Linux, install standalone:
#   curl -fsSL https://github.com/PowerShell/PowerShell/releases/download/v7.4.6/powershell-7.4.6-linux-x64.tar.gz -o /tmp/pwsh.tar.gz
#   mkdir -p ~/.local/pwsh && tar -xzf /tmp/pwsh.tar.gz -C ~/.local/pwsh
#   export PATH="$HOME/.local/pwsh:$PATH"

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

# Locate pwsh: prefer PATH, fall back to the user-local install.
PWSH="${PWSH:-pwsh}"
if ! command -v "${PWSH}" >/dev/null 2>&1; then
    if [[ -x "${HOME}/.local/pwsh/pwsh" ]]; then
        PWSH="${HOME}/.local/pwsh/pwsh"
    else
        echo "ERROR: pwsh not found on PATH and not at ~/.local/pwsh/pwsh." >&2
        echo "Install PowerShell first (see top of this script)." >&2
        exit 2
    fi
fi

# Find every .ps1 file under the repo (skip .venv).
mapfile -t FILES < <(find . -name "*.ps1" -not -path "./.venv/*" -not -path "./.git/*" | sort)
if (( ${#FILES[@]} == 0 )); then
    echo "No .ps1 files found; nothing to lint."
    exit 0
fi

FAILED=0
for f in "${FILES[@]}"; do
    # 1. Non-ASCII check — Windows PowerShell 5.x mis-parses UTF-8 files
    # without a BOM, so fancy quotes / em-dashes etc. break tokenization.
    if LC_ALL=C grep -nP "[^\x00-\x7F]" "$f" >/dev/null 2>&1; then
        echo "FAIL ${f}: contains non-ASCII characters (will break Windows PS 5.x):"
        LC_ALL=C grep -nP "[^\x00-\x7F]" "$f" | head -5
        FAILED=1
        continue
    fi

    # 2. Parse with PowerShell's actual parser. Reports unclosed braces,
    # syntax errors, etc.
    OUT="$("${PWSH}" -NoProfile -Command "
        \$tokens = \$null; \$errors = \$null
        \$null = [System.Management.Automation.Language.Parser]::ParseFile(
            '$PWD/$f', [ref]\$tokens, [ref]\$errors)
        if (\$errors.Count -gt 0) {
            foreach (\$e in \$errors) { Write-Host (\$e.ToString()) }
            exit 1
        }
    " 2>&1)" || {
        echo "FAIL ${f}: parse error:"
        echo "${OUT}" | sed 's/^/  /'
        FAILED=1
        continue
    }
    echo "ok   ${f}"
done

exit "${FAILED}"
