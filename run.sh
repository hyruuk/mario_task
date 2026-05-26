#!/usr/bin/env bash
# run.sh — launch the Mario experiment.
#
# Behaviour:
#   - First run (no config.json): opens the config wizard, then exits so
#     the operator can verify and re-launch.
#   - Subsequent runs: opens the per-session subject picker (combobox of
#     existing subjects + free-text new ID), then runs the session.
#
# Usage:
#   bash run.sh                       # interactive (wizard / subject picker)
#   bash run.sh sub01 01              # skip subject picker; use these labels
#
# Env-var overrides (rare; see .env.example for the full list):
#   MARIO_MAX_DURATION=30 bash run.sh   # short run, useful for smoke testing

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-${ROOT_DIR}/.venv}"
PY="${VENV_DIR}/bin/python"
LOCAL_LIB_DIR="${ROOT_DIR}/.local-libs"

if [[ ! -x "${PY}" ]]; then
  echo "venv not found at ${VENV_DIR}; run 'bash setup_env.sh' first." >&2
  exit 1
fi

# Use bundled libportaudio if the system one isn't installed.
if [[ -e "${LOCAL_LIB_DIR}/libportaudio.so.2" ]]; then
  export LIBRARY_PATH="${LOCAL_LIB_DIR}${LIBRARY_PATH:+:${LIBRARY_PATH}}"
  export LD_LIBRARY_PATH="${LOCAL_LIB_DIR}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
fi

cd "${ROOT_DIR}"
exec "${PY}" -m mario_task "$@"
