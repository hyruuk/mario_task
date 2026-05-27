#!/usr/bin/env bash
# setup_env.sh — first-time install on Linux.
#
# Idempotent: re-running it after a successful install is a no-op (apt
# packages get a "nothing to install" pass, the venv is reused, the
# mario.stimuli checkout is left alone, and the smoke test re-runs).
#
# Tested on Linux Mint 22.2 (upstream Ubuntu 24.04 noble), Python 3.10.
#
# Usage:
#   bash setup_env.sh                    # full install (apt + venv + data)
#   SKIP_APT=1 bash setup_env.sh         # skip the apt step, use bundled libportaudio
#   PYTHON_VERSION=3.10 bash setup_env.sh
#
# After the script finishes:
#   bash run.sh                          # launches the first-run config wizard

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-${ROOT_DIR}/.venv}"
PYTHON_VERSION="${PYTHON_VERSION:-3.10}"
LOCAL_LIB_DIR="${ROOT_DIR}/.local-libs"
MARIO_STIMULI_DIR="${ROOT_DIR}/data/mario.stimuli"

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m[!]\033[0m %s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# Detect the upstream Ubuntu release.
# Linux Mint / Pop!_OS report their own version with lsb_release; we want the
# upstream Ubuntu number for the wxPython extras URL.
# ---------------------------------------------------------------------------
detect_ubuntu_version() {
  if [[ -r /etc/upstream-release/lsb-release ]]; then
    awk -F= '/DISTRIB_RELEASE/{print $2}' /etc/upstream-release/lsb-release
    return
  fi
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}" in
      noble)  echo 24.04; return ;;
      jammy)  echo 22.04; return ;;
      focal)  echo 20.04; return ;;
    esac
  fi
  lsb_release -rs 2>/dev/null || echo 24.04
}
UBUNTU_VERSION="$(detect_ubuntu_version)"
WX_FIND_LINKS="${WX_FIND_LINKS:-https://extras.wxpython.org/wxPython4/extras/linux/gtk3/ubuntu-${UBUNTU_VERSION}/}"
log "Detected upstream Ubuntu ${UBUNTU_VERSION} (wxPython wheels: ${WX_FIND_LINKS})"

# ---------------------------------------------------------------------------
# 1. System (apt) dependencies
# ---------------------------------------------------------------------------
# libwebkit2gtk was renamed 4.0 -> 4.1 in Ubuntu 24.04 (noble).
if dpkg --compare-versions "${UBUNTU_VERSION}" ge 24.04 2>/dev/null; then
  WEBKIT_PKG="libwebkit2gtk-4.1-dev"
else
  WEBKIT_PKG="libwebkit2gtk-4.0-dev"
fi

APT_PACKAGES=(
  # build toolchain
  build-essential pkg-config cmake swig git curl ca-certificates
  # python build deps
  python3-dev python3-venv libffi-dev libssl-dev
  # PsychoPy / wxPython
  libsdl2-dev libsdl2-2.0-0
  libgtk-3-dev
  "${WEBKIT_PKG}"
  libnotify-dev libxtst-dev libsm-dev
  freeglut3-dev libglu1-mesa-dev libegl1-mesa-dev libgles2-mesa-dev
  libxkbcommon-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
  # audio
  portaudio19-dev libasound2-dev libpulse-dev libsndfile1-dev
  # video / image
  ffmpeg libavformat-dev libavcodec-dev libavutil-dev libswscale-dev
  libjpeg-dev libpng-dev libtiff-dev
  # USB / serial
  libusb-1.0-0-dev
  # stable-retro deps
  zlib1g-dev libbz2-dev liblzma-dev
  # X / fonts (psychopy text rendering)
  libxcb-xinerama0 libxrandr-dev libxinerama-dev libfreetype-dev fonts-dejavu-core
  # ROM data acquisition. datalad is a Python dep (installed via uv sync below),
  # but git-annex is a C/Haskell binary that must come from apt.
  git-annex
)

apt_install_if_missing() {
  local missing=()
  for pkg in "$@"; do
    if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
      missing+=("$pkg")
    fi
  done
  if (( ${#missing[@]} == 0 )); then
    log "All required apt packages already installed."
    return 0
  fi
  log "Installing ${#missing[@]} apt packages (requires sudo): ${missing[*]}"
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
}

if [[ "${SKIP_APT:-0}" == "1" ]]; then
  warn "SKIP_APT=1 — skipping system package installation."
else
  if sudo -n true 2>/dev/null || sudo true; then
    apt_install_if_missing "${APT_PACKAGES[@]}"
  else
    warn "sudo unavailable — skipping apt step. Falling back to local libportaudio."
    SKIP_APT=1
  fi
fi

# ---------------------------------------------------------------------------
# 1b. No-sudo fallback: extract libportaudio2 from a downloaded .deb so
#     sounddevice can dlopen() it via LIBRARY_PATH.
# ---------------------------------------------------------------------------
if [[ "${SKIP_APT:-0}" == "1" ]] || ! ldconfig -p 2>/dev/null | grep -q libportaudio.so.2; then
  if ! [[ -e "${LOCAL_LIB_DIR}/libportaudio.so.2" ]]; then
    log "Fetching libportaudio2 to ${LOCAL_LIB_DIR} (no sudo needed)"
    mkdir -p "${LOCAL_LIB_DIR}"
    tmp_d=$(mktemp -d)
    (cd "$tmp_d" && apt-get download libportaudio2 >/dev/null)
    deb_file="$(ls "$tmp_d"/libportaudio2*.deb)"
    extract_d="$tmp_d/extracted"
    mkdir -p "$extract_d"
    dpkg -x "$deb_file" "$extract_d"
    so_real=$(find "$extract_d" -name 'libportaudio.so.2.*' -print -quit)
    cp -L "$so_real" "${LOCAL_LIB_DIR}/"
    so_basename=$(basename "$so_real")
    ln -sfn "$so_basename" "${LOCAL_LIB_DIR}/libportaudio.so.2"
    ln -sfn "$so_basename" "${LOCAL_LIB_DIR}/libportaudio.so"
    rm -rf "$tmp_d"
    log "libportaudio fallback installed at ${LOCAL_LIB_DIR}"
  fi
fi

# ---------------------------------------------------------------------------
# 2. uv (https://github.com/astral-sh/uv)
# ---------------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  log "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
log "uv $(uv --version)"

# ---------------------------------------------------------------------------
# 3. Virtual environment + project install
# ---------------------------------------------------------------------------
# Vendor + patch stable-retro into .cache/stable-retro. pyproject.toml's
# [tool.uv.sources] points stable-retro at this path. The patch only
# changes behavior on Windows (skips the fbneo + parallel_n64 cores
# where upstream's Windows build is broken) -- on Linux it's a no-op,
# but we still need the local checkout so uv lock / uv sync find it.
STABLE_RETRO_DIR="${ROOT_DIR}/.cache/stable-retro"
STABLE_RETRO_REV="24180f5dcc4ec3ba725f9614823c22ef5c6983ff"
STABLE_RETRO_REPO="https://github.com/Farama-Foundation/stable-retro"
if [[ ! -d "${STABLE_RETRO_DIR}/.git" ]]; then
  log "Cloning stable-retro into ${STABLE_RETRO_DIR} (~600 MB with submodules)"
  mkdir -p "$(dirname "${STABLE_RETRO_DIR}")"
  rm -rf "${STABLE_RETRO_DIR}"
  git clone "${STABLE_RETRO_REPO}" "${STABLE_RETRO_DIR}"
fi
( cd "${STABLE_RETRO_DIR}"
  git fetch --depth 1 origin "${STABLE_RETRO_REV}" 2>/dev/null || true
  git checkout "${STABLE_RETRO_REV}"
  git submodule update --init --recursive --depth 1 )

# Apply the fbneo skip patch. Idempotent via the marker.
CMAKE_FILE="${STABLE_RETRO_DIR}/CMakeLists.txt"
if ! grep -q "mario_task-patch: skip fbneo" "${CMAKE_FILE}"; then
  log "Patching stable-retro/CMakeLists.txt to skip fbneo + parallel_n64 on Windows"
  # Widen the macOS skip-branch to also catch WIN32.
  python3 - <<PYEOF
import pathlib, re, sys
p = pathlib.Path(r"${CMAKE_FILE}")
text = p.read_text()
old = 'if(APPLE)\n  message(\n    WARNING\n      "FBNeo arcade and parallel N64 emulator is currently not supported on macOS"\n  )\nelse()\n  add_core(fbneo fbneo)'
new = 'if(APPLE OR WIN32) # mario_task-patch: skip fbneo + parallel_n64 on Windows\n  message(\n    WARNING\n      "FBNeo arcade and parallel N64 emulator is not supported on this platform (mario_task-patch)"\n  )\nelse()\n  add_core(fbneo fbneo)'
if old not in text:
    sys.exit("could not locate the fbneo skip-patch target; upstream layout may have changed")
p.write_text(text.replace(old, new))
PYEOF
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  log "Creating virtualenv at ${VENV_DIR} (python ${PYTHON_VERSION})"
  uv venv --python "${PYTHON_VERSION}" "${VENV_DIR}"
fi

PY="${VENV_DIR}/bin/python"
PIP_INSTALL=(uv pip install --python "${PY}")

log "Installing mario_task and pinned deps from uv.lock (--extra dev)"
( cd "${ROOT_DIR}" && uv sync --extra dev )

# ---------------------------------------------------------------------------
# 4. wxPython sanity check.
# ---------------------------------------------------------------------------
# pyproject.toml's [tool.uv.sources] pins wxPython to the extras-index wheel
# on Linux + Python 3.10 (4.2.2 built against Ubuntu 24.04's libwx 3.2.4).
# uv sync above installs it. We just verify here.
if ! "${PY}" - <<'PYEOF' 2>/dev/null
import wx  # noqa: F401
from wx import App  # noqa: F401
PYEOF
then
  warn "wxPython fails to import — the extras-index pin in pyproject.toml may be stale."
  warn "On Ubuntu 24.04 the system libwx is 3.2.4; PyPI wxPython 4.2.5 needs 3.2.6+."
  warn "Check [tool.uv.sources] in pyproject.toml — the pinned URL must match your"
  warn "upstream Ubuntu version (${UBUNTU_VERSION}) and Python version."
  exit 1
fi
log "wxPython $("${PY}" -c 'import wx; print(wx.version())') imports cleanly."

# ---------------------------------------------------------------------------
# 5. ROM data via datalad (anonymous HTTPS — no SSH key required)
# ---------------------------------------------------------------------------
MARIO_ROM="${MARIO_STIMULI_DIR}/SuperMarioBros-Nes/rom.nes"
if [[ ! -e "${MARIO_STIMULI_DIR}/.git" ]]; then
  log "Cloning mario.stimuli via datalad (anonymous HTTPS, no credentials)"
  mkdir -p "$(dirname "${MARIO_STIMULI_DIR}")"
  "${VENV_DIR}/bin/datalad" install -s https://github.com/courtois-neuromod/mario.stimuli "${MARIO_STIMULI_DIR}"
else
  log "mario.stimuli already cloned at ${MARIO_STIMULI_DIR}"
fi

if [[ ! -s "${MARIO_ROM}" ]]; then
  log "Fetching ROM + level save-states via datalad get (public conp-ria-storage-http)"
  ( cd "${MARIO_STIMULI_DIR}" && "${VENV_DIR}/bin/datalad" get . )
fi

if [[ ! -s "${MARIO_ROM}" ]]; then
  warn "rom.nes is still empty after datalad get — the remote mirror may be down."
  warn "Try manually: cd ${MARIO_STIMULI_DIR} && datalad get ."
  exit 1
fi
log "ROM is real: $("${PY}" -c "import os; print(os.path.getsize(r'${MARIO_ROM}'), 'bytes')")"

# ---------------------------------------------------------------------------
# 6. Smoke test
# ---------------------------------------------------------------------------
log "Smoke testing imports..."
LIB_OVERRIDE=""
if [[ -e "${LOCAL_LIB_DIR}/libportaudio.so.2" ]]; then
  LIB_OVERRIDE="LIBRARY_PATH=${LOCAL_LIB_DIR} LD_LIBRARY_PATH=${LOCAL_LIB_DIR}"
fi
env ${LIB_OVERRIDE} "${PY}" - <<'PYEOF'
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
if not ok:
    sys.exit(1)
PYEOF

log "Done. Activate the environment with:"
echo "    source ${VENV_DIR}/bin/activate"
if [[ -e "${LOCAL_LIB_DIR}/libportaudio.so.2" ]]; then
  echo "    export LD_LIBRARY_PATH=${LOCAL_LIB_DIR}:\$LD_LIBRARY_PATH"
  echo "    (or just run ./run.sh which sets it automatically)"
fi
echo
echo "Launch the experiment with:"
echo "    bash run.sh"
echo "First launch will open the configuration wizard."
