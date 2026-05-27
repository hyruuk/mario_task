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

## How to run a session

1. **First run only:** a config wizard opens to set the trigger backend (LSL / serial / parallel / none), port, and run duration. Saves to `config.json`.
2. **Every run:** a subject-picker dialog opens. Pick an existing subject (auto-resumes from their savestate) or type a new ID.
3. The Mario task starts. Controls: **arrow keys** to move, **Z** to run, **X** to jump.
4. After each run, a prompt appears: **X** to continue with another run, **Z** to end the session.
5. **Ctrl+C** during a run aborts cleanly without advancing the savestate.

## Serial trigger permissions

If the markers backend is set to `serial` (e.g. `/dev/ttyUSB0`) and you see:

```
mario_task.markers WARNING: Falling back to NullBackend (markers will be dropped):
[Errno 13] could not open port /dev/ttyUSB0: [Errno 13] Permission denied
```

your user is not in the `dialout` group. Add it once:

```bash
sudo usermod -aG dialout $USER
```

Then log out and back in (or reboot) for the new group to take effect. Verify with `groups | grep dialout`.

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
