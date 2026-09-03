# mario_task

A clean, reproducible experiment runner for NES *Super Mario Bros* with EEG / iEEG marker support and BIDS-compatible outputs.

> Status: under active construction. Phase 0 (skeleton + pure-Python core + tests) is the current milestone. See [the implementation plan](../.claude/plans/) for the full roadmap.

## What it does

- Runs a Mario paradigm with two phases:
  - **Discovery** — one level per run, replayed for the full run duration. Levels advance run-by-run (Level 1-1 → 1-2 → 1-3 → 2-1 → … → 8-3, skipping (2,2), (7,2), and all X-4 castle levels).
  - **Practice** — 22 shuffled levels per run, repeating until the session is ended.
- Variable-length sessions: after every run, the operator presses **X** to start the next run or **Z** to end the session.
- Streams EEG markers per emulator frame via LSL (default), serial, or parallel port.
- Aligns the start of each run with the recording device: wait for a scanner trigger, or send one.
- Writes BIDS-compatible logs, BK2 emulator recordings, and per-task events TSVs.

## Quick install (Linux)

```bash
git clone <this-repo> ~/GitHub/mario_task
cd ~/GitHub/mario_task
bash setup_env.sh        # installs system deps, venv, fetches ROM via datalad
bash run.sh sub01 01     # first run launches the config wizard
```

## How to run a session

1. **First run only:** a config wizard opens, split across tabs — Session, Display, Levels, Scanner sync, Markers, Game data. Every field has an `ⓘ` explaining it on hover. Saves to `config.json`; re-open it any time with `bash run.sh --reconfigure`, which pre-fills the current values.
2. **Every run:** a subject-picker dialog opens. Pick an existing subject (auto-resumes from their savestate) or type a new ID.
3. The Mario task starts. Default controls: **arrow keys** to move, **Z** to run, **X** to jump — rebindable on the wizard's Controls tab.
4. After each run, a prompt appears: **X** to continue with another run, **Z** to end the session.
5. **Ctrl+C** during a run aborts cleanly without advancing the savestate; **Ctrl+Q** ends the session. See [Operator shortcuts](#operator-shortcuts).

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

## Configuration

Everything lives in `config.json` (written by the wizard). Re-open the wizard with
`bash run.sh --reconfigure`. Values are layered, **later wins**:

```
defaults  <  config.json  <  environment / .env  <  CLI flags
```

Field labels in the wizard are just the `config.json` key, so the dialog and the file
read alike; the `ⓘ` at the end of each row says what the setting does, so setting up a
rig does not mean reading this file in another window.

The `sync` and `triggers` sections are shared, name for name, with
[`controller_validation_task`](../controller_validation_task) — one rig setup, two tasks.

### Scanner start signal — `sync`

| `mode` | Behaviour |
| --- | --- |
| `none` | Start immediately. The default. |
| `wait` | Hold on a "Waiting for the scanner" screen until the sync signal arrives. |
| `send` | Emit `signal` once at run start. Use when the stimulus computer starts the scanner. |

Sync happens once per gameplay run, and **exactly one thing gates the start of a run — the
one that owns the screen**:

| `mode` | What the subject sees |
| --- | --- |
| `wait` | Only "Waiting for the scanner". The "press X when ready" screen is skipped: the trigger has already released the run, so asking for a second go-ahead would strand the subject in a live sequence. |
| `send` | "Press X when ready" first, then the start pulse goes out — so the scanner is not left running while they read. |
| `none` | Only "press X when ready". |

The waiting screen names no keys: which signal is being watched for is operator information,
and goes to the console and session log. The end-of-run prompt is never synced — it is the
operator answering a question, not a run to align.

`backend` says *over what*, and **defaults to `none`** — no hardware. In `wait` mode the
signal is then expected from the keyboard; most MR trigger boxes present as a USB keyboard,
so this covers the scanner as well as the desk. In `send` mode there is nothing to send to,
so the run starts with a warning.

`sync.signal` is the signal itself, and means the same thing both ways: in `send` mode it is
what goes out (`"s"` → the byte 115), in `wait` mode it is what we listen for. Default `"s"`.
List alternatives when one physical key reports under several names — `["5", "percent"]` is
the same trigger-box key with and without shift. Only the first entry is ever sent.

Otherwise, for `send`: `serial`, `parallel`, `lsl`, `key`, or `markers` (re-use the
already-open marker port, so one serial device carries both). For `wait`: `keyboard` or
`serial`.

**No port, no problem.** If `sync.port` is unset — or the port refuses to open — the session
warns and degrades to the `none` behaviour for that mode: `wait` listens on the keyboard,
`send` starts the run unsynchronised. The same `config.json` therefore works at the scanner
and on a desk. The waiting screen shows only `Waiting for the scanner`; which keys are being
watched is printed to the console and the session log, for the operator.

Send `s` to a serial port at run start:

```bash
bash run.sh 01 001 --sync-mode send --sync-backend serial --sync-port /dev/ttyUSB0
```

### Controls — `input`

The pad is read as a keyboard, so the whole of the controller configuration is which key
drives which NES button. Set it on the wizard's **Controls** tab, or in `config.json`:

```json
"input": { "button_map": { "UP": "up", "DOWN": "down", "LEFT": "left",
                           "RIGHT": "right", "A": "x", "B": "z",
                           "START": "", "SELECT": "" } }
```

Defaults are the classic layout the task has always used: **arrows** to move, **Z** to run
(B), **X** to jump (A). Key names are what pyglet reports, lowercased — arrows are
`up`/`down`/`left`/`right`, letters and digits are themselves. An fMRI-compatible pad
usually presents as a USB keyboard, so bind whatever keys yours actually sends.

`START` and `SELECT` are unbound by default and best left that way: a subject who can pause
mid-level produces a recording nobody can segment. Blank means unbound.

Two mistakes are refused before a session starts: leaving one of the six playable buttons
unbound, and binding one key to two buttons (which would press both at once). A partial
`button_map` in `config.json` merges onto the defaults, so rebinding one button does not
silently unbind the rest.

The end-of-run questionnaire is navigated with the same keys — it reads UP/DOWN/LEFT/RIGHT
and A straight out of this mapping, so remapping the pad remaps the questionnaire with it.

### Event markers — `triggers`

Independent of `sync`: a run can wait for a scanner and also emit markers, or do neither.
Backends: `lsl` (the default, recommended for iEEG), `serial`, `parallel`, `null`.

`codes` says what value each event sends; the `on_*` switches say whether it is sent at all.
Task start and stop always fire — without them a recording cannot be segmented.

| Setting | What it controls |
| --- | --- |
| `on_game_frame` | One marker per emulator frame — the bulk of the stream. |
| `on_game_reset` | One marker at the start of every attempt. |
| `on_non_game_flip` | Heartbeat on instructions / fixation / questionnaire flips. |
| `trigger_every` | Send one gameplay marker per N frames. Raise it to relieve a saturated amplifier; the bk2 still records every frame. |

Check the chain before a participant arrives:

```bash
uv run python -m mario_task.monitor    # prints decoded markers live
```

A trigger backend that fails to open **never aborts the session** — it logs a warning and
downgrades to dropping markers.

## Operator shortcuts

| Keys | Effect |
| --- | --- |
| `Ctrl+C` | abort the current run, move to the next |
| `Ctrl+Q` | quit the session |
| `Ctrl+N` | reserved for "restart this run"; not implemented yet — it ends the run and says so |

They are live in every phase: the instructions screen, the `Waiting for the scanner`
screen, mid-level, the questionnaire and the end-of-run prompt. Quitting is a clean exit,
not a kill: the run's events file and bk2 are written, the log is flushed, and the window,
sync port and marker backend are closed in order. The process exits `130`.

`c` / `n` / `q` need Ctrl because a bare letter is more likely to be a stray keystroke than
a decision — and during a level the subject's unmodified keystrokes belong to the emulator
and never reach PsychoPy at all. `Ctrl+Q` is the only clean exit; `Escape` does nothing.

`Ctrl+Q` is additionally registered as a PsychoPy global key, so it is caught the moment it
arrives rather than only on the frames the shortcut poller runs on — including during the
blocking stretches around emulator setup and savestate loads. It still needs the experiment
window to have keyboard focus; it is not an OS-level hotkey.

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
| `session.py` | Window + log + marker + sync lifetimes, task loop, Ctrl+C/N/Q handling. |
| `markers.py` | EEG markers — LSL / serial / parallel backends. |
| `sync.py` | Run-start synchronisation with the scanner (send / wait). |
| `design.py` | Deterministic per-subject level shuffle (sha1 seed). |
| `savestate.py` | Atomic JSON read/write for cross-session progress. |
| `paths.py` | BIDS path resolution, ROM/state presence checks. |
| `phases.py` | Discovery / practice phase generator (`iter_tasks`). |
| `engine.py` | retro + psychopy frame loop (verbatim port of upstream's `_run_emulator`). |
| `task.py` | `MarioTask` + `Pause` + `EndOfRunPrompt` lifecycle. |
| `audio.py` | Thread-safe NES audio playback. |
| `input.py` | Pyglet keypress/release interleaver; `settings.InputSettings` binds the keys. |
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
