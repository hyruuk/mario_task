# mario_task

A clean, reproducible experiment runner for NES *Super Mario Bros* with EEG / iEEG marker support and BIDS-compatible outputs.

> Status: under active construction. Phase 0 (skeleton + pure-Python core + tests) is the current milestone. See [the implementation plan](../.claude/plans/) for the full roadmap.

## What it does

- Runs a Mario paradigm with two phases:
  - **Discovery** — one level per run, replayed for the full run duration. Levels advance run-by-run (Level 1-1 → 1-2 → 1-3 → 2-1 → … → 8-3, skipping (2,2), (7,2), and all X-4 castle levels).
  - **Practice** — 22 shuffled levels per run, repeating until the session is ended.
- Variable-length sessions: after every run, the operator presses **X** to start the next run or **Z** to end the session.
- Streams EEG markers per emulator frame via LSL (default), serial, or parallel port.
- Writes BIDS-compatible logs, BK2 emulator recordings, and per-task events TSVs.

## Quick install (Linux)

```bash
git clone <this-repo> ~/GitHub/mario_task
cd ~/GitHub/mario_task
bash setup_env.sh        # installs system deps, venv, fetches ROM via datalad
bash run.sh sub01 01     # first run launches the config wizard
```

## Quick install (Windows)

For operators on a Windows 10 / 11 deploy box. **Internet required** for the first run (~1.5 GB of downloads: Python, Git, git-annex, Python deps, the Mario ROM).

1. Download the latest **mario_task-windows-vX.Y.Z.zip** from the [Releases page](../../releases/latest).
2. Move the ZIP somewhere stable (e.g. `Documents` or `My PC → This PC → C:`). Avoid `Downloads` — Windows may auto-clean it.
3. Right-click the ZIP → **Extract All**.
4. Open the extracted folder and **double-click `install.bat`**.
   - If Windows SmartScreen warns about an unknown publisher, click **More info** → **Run anyway**.
   - If a UAC prompt appears, click **Yes**.
5. Wait for **"Setup complete!"**. First run is ~5 minutes (Python install + dependency resolve + ROM download).
6. A new **"Run Mario Task"** shortcut appears on your desktop. Double-click it to launch.
7. **First launch only** — fill in the configuration wizard (trigger backend, enabled levels, etc.) and click Save.
8. **Every session** — the subject picker opens. Pick or type a subject label, click Start session.

### Troubleshooting (Windows)

| Symptom | Fix |
| --- | --- |
| SmartScreen blocks `install.bat` | Click "More info" then "Run anyway". The script is unsigned because we don't publish through the Microsoft Store. |
| "winget is not available" | On Windows 10, install [App Installer from the Microsoft Store](https://www.microsoft.com/store/productId/9NBLGGH4NNS1), then re-run `install.bat`. On Windows 11 it's preinstalled — try rebooting. |
| `setup_env.ps1` fails on git-annex install | Install [git-annex for Windows](https://git-annex.branchable.com/install/Windows/) manually, then re-run `install.bat`. |
| ROM download hangs / fails | The conp-ria-storage-http mirror is occasionally slow. Re-run `install.bat` (it picks up where it left off). |
| Antivirus blocks PowerShell | Add an exclusion for the extracted folder. Common with corporate Windows images. |
| Black PsychoPy window / very low frame rate | Graphics driver issue. Update the GPU driver via Device Manager; if you're testing inside a VM, enable 3D acceleration in the VM settings. |

### Updating to a new release

Each release is a fresh ZIP. To upgrade an existing install without losing subject data:

1. **Back up your data.** Copy `output\` and `config.json` from your install folder to a safe location (e.g. Desktop).
2. Delete the old install folder.
3. Download and extract the new ZIP; run `install.bat`.
4. **Restore your data.** Copy `output\` and `config.json` back into the new install folder. Subjects resume from where they left off.

`data\mario.stimuli\` doesn't need backing up — `install.bat` re-fetches the ROM via datalad on first run.

## How to run a session

1. **First run only:** a config wizard opens to set the trigger backend (LSL / serial / parallel / none), port, and run duration. Saves to `config.json`.
2. **Every run:** a subject-picker dialog opens. Pick an existing subject (auto-resumes from their savestate) or type a new ID.
3. The Mario task starts. Controls: **arrow keys** to move, **Z** to run, **X** to jump.
4. After each run, a prompt appears: **X** to continue with another run, **Z** to end the session.
5. **Ctrl+C** during a run aborts cleanly without advancing the savestate.

## Output layout

```
output/
└── sourcedata/sub-XX/
    ├── sub-XX_phase-discovery_task-mario_savestate.json
    ├── sub-XX_phase-stable_task-mario_savestate.json
    └── ses-YY/
        ├── sub-XX_ses-YY_YYYYMMDD-HHMMSS.log
        ├── sub-XX_ses-YY_*_task-mario_*_events.tsv
        └── sub-XX_ses-YY_*_task-mario_*.bk2
```

Per-subject level designs live at `data/videogames/mario/designs/sub-XX_design.tsv`.

## Architecture

The Python package lives in [`mario_task/`](mario_task/). Each module has one job:

| Module | Responsibility |
| --- | --- |
| `cli.py` | Parse args, load config, dispatch to `session.run_session`. |
| `session.py` | Window setup, log file lifetime, task loop, Ctrl+N/C/Q handling. |
| `markers.py` | EEG markers — LSL / serial / parallel backends. |
| `design.py` | Deterministic per-subject level shuffle (sha1 seed). |
| `savestate.py` | Atomic JSON read/write for cross-session progress. |
| `paths.py` | BIDS path resolution, ROM/state presence checks. |
| `phases.py` | Discovery / practice phase generator (`iter_tasks`). |
| `engine.py` | retro + psychopy frame loop (verbatim port of upstream's `_run_emulator`). |
| `task.py` | `MarioTask` + `Pause` + `EndOfRunPrompt` lifecycle. |
| `audio.py` | Thread-safe NES audio playback. |
| `input.py` | Pyglet keypress/release interleaver. |
| `questionnaire.py` | Likert UI for post-run flow ratings. |
| `log_setup.py` | PsychoPy LogFile lifetime + flush policy. |
| `settings.py` | `config.json` schema + override hierarchy. |
| `gui.py` | First-run config wizard + per-session subject picker. |

## Development

```bash
just test          # pure-Python unit tests (no display required)
just test-integration   # display-required smoke test
just lint
just lock          # re-resolve uv.lock
```

## License

MIT.
