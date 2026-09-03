"""Session orchestrator — Phase 1 version.

This module owns:
    * the PsychoPy window lifetime
    * the PsychoPy LogFile lifetime
    * the EEG marker backend lifetime
    * the scanner-sync transport lifetime
    * the retro custom-path registration
    * the task lifecycle loop (setup → instructions → run → stop → save)

Exit codes:

===  ======================================================
0    session completed (or was ended cleanly by the operator)
2    the ROM / state data is missing or unusable
130  the operator quit with Ctrl+Q
===  ======================================================

Operator shortcuts, live during every phase — instructions, the scanner
wait, gameplay, the questionnaire and the end-of-run prompt:

======  ==========================================================
Ctrl+C  abort this run, continue to the next
Ctrl+Q  quit the session
Ctrl+N  reserved for "restart this run"; currently not acted on
======  ==========================================================
"""

from __future__ import annotations

import logging as stdlogging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psychopy.logging as psy_logging
import retro
from psychopy import core, event, logging, visual

from mario_task import design, log_setup, markers, phases
from mario_task import sync as sync_mod
from mario_task.paths import BidsPaths, check_data_root
from mario_task.questionnaire import build_default_questions
from mario_task.settings import Settings
from mario_task.task import EndOfRunPrompt, MarioTask, _TaskBase

#: Stdlib logger for operator-facing diagnostics. Distinct from the ``logging``
#: imported from psychopy above, which writes the session .log file.
log = stdlogging.getLogger(__name__)


# Quiet PsychoPy's verbose frame-drop logging during gameplay (we already
# log dropped frames ourselves in engine.run_emulator).
visual.window.reportNDroppedFrames = 10**10  # type: ignore[attr-defined]


@dataclass
class RunConfig:
    """Everything ``session.run_session`` needs in one bag."""

    subject: str
    session: str
    settings: Settings
    paths: BidsPaths
    log_file: psy_logging.LogFile | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Window construction
# ---------------------------------------------------------------------------


def _detect_screen_geometry() -> tuple[int, int, int]:
    """Return ``(width, height, screen_index)`` of the best available display.

    Mirrors upstream config.py's behaviour: try the screen requested by
    EXP_WIN_SCREEN, fall back to the last available screen index.
    """
    try:
        import pyglet

        screens = pyglet.canvas.Display().get_screens()
    except Exception:
        return 1920, 1080, 0
    if not screens:
        return 1920, 1080, 0
    requested = int(os.environ.get("EXP_WIN_SCREEN", 0))
    idx = requested if requested < len(screens) else len(screens) - 1
    s = screens[idx]
    return s.width, s.height, idx


def _build_window(settings: Settings) -> visual.Window:
    """Open the PsychoPy experiment window per settings.display + env vars."""
    w_default, h_default, screen_default = _detect_screen_geometry()
    win_size = settings.display.window_size or (w_default, h_default)
    screen_idx = (
        settings.display.screen_index
        if settings.display.screen_index is not None
        else screen_default
    )
    fullscreen = settings.display.fullscreen
    win = visual.Window(
        size=win_size,
        screen=screen_idx,
        fullscr=fullscreen,
        color=(-1, -1, -1),
        colorSpace="rgb",
        gammaErrorPolicy="warn",
        units="pix",
        allowGUI=not fullscreen,
    )
    win.mouseVisible = False
    # Pyglet otherwise grabs sys.argv[0] for the title bar, leaking the
    # launcher script name into the window if it ends up non-fullscreen.
    try:
        win.winHandle.set_caption("mario_task")
    except Exception:
        pass
    return win


# ---------------------------------------------------------------------------
# Keyboard shortcuts
# ---------------------------------------------------------------------------


#: Set by the Ctrl+Q global key; consumed by :func:`_listen_shortcuts`.
_quit_requested = False


def _request_quit() -> None:
    """Remember a Ctrl+Q. Called by PsychoPy the instant the key is pressed.

    It only raises a flag — quitting still goes through the normal shortcut
    path, so the run's events file and bk2 are written and every backend is
    closed in order. Calling ``core.quit()`` here would kill the process
    mid-level and lose the run.
    """
    global _quit_requested
    _quit_requested = True


def _install_quit_key() -> None:
    """Register Ctrl+Q with PsychoPy's global key handler.

    :func:`_listen_shortcuts` only sees a keypress on a frame it is polled on;
    a global key is dispatched by PsychoPy the moment the key arrives, so a
    Ctrl+Q during a blocking stretch — emulator setup, a savestate load, the
    gap between two attempts — is remembered rather than dropped.

    It is not an OS-level hotkey: the experiment window still has to have
    keyboard focus, because PsychoPy dispatches these from the same pyglet
    handler as everything else. That handler is
    :func:`mario_task.input._on_pyglet_key_press` during gameplay, which
    forwards modified keys onward — which is what keeps Ctrl+Q alive mid-level.
    """
    global _quit_requested
    _quit_requested = False
    try:
        event.globalKeys.add(key="q", modifiers=["ctrl"], func=_request_quit, name="quit")
    except Exception:  # noqa: BLE001 - already registered, or no global keys
        log.debug("Could not register the Ctrl+Q global key.", exc_info=True)


def _remove_quit_key() -> None:
    """Unregister Ctrl+Q. ``globalKeys`` outlives the session otherwise."""
    try:
        event.globalKeys.remove("q", modifiers=["ctrl"])
    except Exception:  # noqa: BLE001 - never registered, or already gone
        pass


def _listen_shortcuts() -> str | None:
    """Return ``"c"`` / ``"n"`` / ``"q"``, or ``None`` if nothing was pressed.

    Every shortcut requires Ctrl, without exception: during gameplay the
    subject's unmodified keystrokes are captured by :mod:`mario_task.input`,
    and a bare key is far more likely to be a stray press than a decision.
    Ctrl+Q is the one way out, and :func:`mario_task.input._on_pyglet_key_press`
    forwards modified keys to PsychoPy precisely so it keeps working mid-level.

    The modifier is checked **per key**. An earlier version computed "was any
    ctrl held" across the whole batch and then returned the *first* key's name,
    so a stray unmodified 'q' turned the operator's next Ctrl+C into a session
    quit — and because that version only called ``getKeys`` when a ctrl key was
    already buffered, the stray 'q' was never drained and sat there waiting.

    Passing an explicit key list matters: PsychoPy only drops the keys it was
    asked about and leaves the rest in the buffer, so polling for shortcuts
    every frame cannot swallow the sync signal the scanner waiter is watching
    for on those same frames.
    """
    global _quit_requested
    if _quit_requested:
        _quit_requested = False
        return "q"

    for name, mods in event.getKeys(["n", "c", "q"], modifiers=True):
        if mods.get("ctrl"):
            return name
    return None


def _run_task_loop(task_gen) -> str | None:
    """Drive a generator-per-frame phase. Returns the shortcut that broke it, if any."""
    for frame_n, _ in enumerate(task_gen):
        shortcut = _listen_shortcuts()
        if shortcut:
            return shortcut
        # Force regular log flushing so a hard crash keeps the last second of telemetry.
        if frame_n % 60 == 0:
            log_setup.flush()
    return None


def _run_task(
    task: _TaskBase,
    exp_win: visual.Window,
    *,
    use_eeg: bool,
    sync_obj: sync_mod.Sync | None = None,
) -> str | None:
    """Drive one task through its lifecycle. Returns the shortcut that ended it."""
    print(f"Next task: {task}")

    # Exactly one thing gates the start of a run, and it owns the screen:
    #
    #   wait - the scanner. The subject sees only "Waiting for the scanner";
    #          a "press X when ready" prompt on top of it would be a second
    #          gate on a run the trigger has already released.
    #   send - us, once the subject is ready: prompt first, then start the
    #          recording, so the scanner is not left running while they read.
    #   none - the subject. The prompt is the whole of it (the desk case).
    #
    # Both screens read the keyboard through event.getKeys, which keeps
    # working until input.install() replaces the pyglet handler inside
    # task.run(). Skipping instructions() also skips its two buffer-clearing
    # flips, which is harmless: run() opens with a clearing flip of its own.
    if sync_obj is not None and sync_obj.waits:
        shortcut = _run_task_loop(sync_obj.start(exp_win))
    else:
        shortcut = _run_task_loop(task.instructions(exp_win, None))
        if sync_obj is not None and not shortcut:
            shortcut = _run_task_loop(sync_obj.start(exp_win))

    logging.info("GO")
    if use_eeg and not shortcut:
        exp_win.callOnFlip(markers.send_signal, markers.TASK_START)

    if not shortcut:
        shortcut = _run_task_loop(task.run(exp_win, None))

    if use_eeg:
        exp_win.callOnFlip(markers.send_signal, markers.TASK_STOP)

    _run_task_loop(task.stop(exp_win, None))
    task.save()
    return shortcut


# ---------------------------------------------------------------------------
# The session entry point
# ---------------------------------------------------------------------------


def run_session(config: RunConfig) -> int:
    """Phase 1: run a single Mario Level1-1 attempt for max_duration seconds.

    Returns the shell exit code (0 = clean, 2 = ROM/data missing, 130 = Ctrl+C / Ctrl+Q).
    """
    # 1. Validate ROM + state data before bringing up any heavy infra.
    data_root = Path(config.settings.paths.data_root)
    err = check_data_root(data_root)
    if err is not None:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    # 2. Pre-create the BIDS output directories.
    config.paths.sourcedata_session_dir.mkdir(parents=True, exist_ok=True)

    # 3. Open the session-wide LogFile. Held as a local for the whole
    #    function scope so it doesn't get GC'd mid-session.
    log_file = log_setup.create_session_log(config.paths.log_path)
    config.log_file = log_file  # caller-visible too, for tests / debugging

    # 4. Configure the EEG marker transport (or NullBackend on failure).
    backend = config.settings.triggers.backend
    use_eeg = backend != "null"
    markers.configure(
        backend=backend,
        port=config.settings.triggers.port,
        stream=markers.StreamConfig(
            name=config.settings.triggers.lsl_stream_name,
            type=config.settings.triggers.lsl_stream_type,
            source_id=config.settings.triggers.lsl_stream_source_id,
        ),
        codes=config.settings.triggers.codes,
        trigger_every=config.settings.triggers.trigger_every,
        events=config.settings.triggers.events(),
    )

    # 4b. Scanner sync. Like markers.configure, this never raises: a port
    #     that is unset or will not open degrades to starting the run
    #     without a sync signal (see mario_task.sync).
    sync_obj = sync_mod.configure(
        config.settings.sync,
        stream=markers.StreamConfig(
            name=config.settings.triggers.lsl_stream_name,
            type=config.settings.triggers.lsl_stream_type,
            source_id=config.settings.triggers.lsl_stream_source_id,
        ),
    )

    # 5. Register the retro custom path so it can find SuperMarioBros-Nes.
    #    Must happen before any retro.make(). retro caches paths and may
    #    re-resolve them at make() time, so we always pass an *absolute*
    #    path here rather than relying on the caller's cwd.
    retro.data.Integrations.add_custom_path(str(data_root.parent.resolve()))

    # 6. Open the PsychoPy window, and the quit key that has to outlive
    #    every phase inside it.
    exp_win = _build_window(config.settings)
    _install_quit_key()

    try:
        # 7. Generate the per-subject design TSV if missing. It lives at
        #    sourcedata/sub-<subject>/sub-<subject>_design.tsv so deleting
        #    the subject dir wipes every trace of the subject (design +
        #    savestates + outputs).
        config.paths.sourcedata_subject_dir.mkdir(parents=True, exist_ok=True)
        design.ensure_design(
            config.paths.design_tsv,
            config.subject,
            enabled_levels=config.settings.task.enabled_levels,
        )

        # 8. Build task factories used by phases.iter_tasks. Each factory
        #    encodes the BIDS task name (including phase + run index)
        #    and the gameplay-specific config knobs.
        post_run_ratings = (
            build_default_questions(include_other=True)
            if config.settings.task.questionnaire_enabled
            else None
        )
        # One positional key list for every task in the session, built from
        # the operator's input.button_map. The questionnaire indexes into it
        # too, so remapping the pad remaps its navigation with it.
        key_set = config.settings.input.key_set()
        fixation_duration = float(config.settings.task.fixation_duration_seconds)
        max_duration = float(config.settings.task.max_duration_seconds)
        subject_q_tsv = config.paths.questionnaire_tsv if post_run_ratings else None

        def make_discovery_task(level_name: str, run_idx: int) -> MarioTask:
            return MarioTask(
                name=f"task-mario_phase-discovery_run-{run_idx + 1:02d}",
                state_names=[level_name],
                max_duration=max_duration,
                repeat_scenario=True,
                key_set=key_set,
                post_run_ratings=post_run_ratings,
                questionnaire_subject_tsv=subject_q_tsv,
                questionnaire_subject_label=config.subject,
                questionnaire_session_label=config.session,
                questionnaire_run_idx=run_idx + 1,
                fixation_duration=fixation_duration,
            )

        def make_practice_task(state_names: list[str], run_idx: int) -> MarioTask:
            return MarioTask(
                name=f"task-mario_phase-stable_run-{run_idx + 1:02d}",
                state_names=list(state_names),
                max_duration=max_duration,
                # repeat_scenario=False: the run ends when either max_duration
                # expires or the design's remaining levels are exhausted.
                # We pass design[index:] as state_names (potentially 1000+
                # levels) and let the task time-cap on its own — never
                # loop back to state_names[0] within the same run.
                repeat_scenario=False,
                key_set=key_set,
                post_run_ratings=post_run_ratings,
                questionnaire_subject_tsv=subject_q_tsv,
                questionnaire_subject_label=config.subject,
                questionnaire_session_label=config.session,
                questionnaire_run_idx=run_idx + 1,
                fixation_duration=fixation_duration,
            )

        def make_prompt() -> EndOfRunPrompt:
            # The run_idx baked into the prompt's name is the *session*-
            # local run counter, not the discovery/practice run index.
            return EndOfRunPrompt(
                run_idx=run_idx_counter[0],
                continue_key="x",
                end_key="z",
                name=f"end-of-run_run-{run_idx_counter[0]:02d}",
            )

        run_idx_counter = [0]  # mutable closure so factories see updates

        # 9. Iterate the phases generator. It yields a sequence of
        #    MarioTask / EndOfRunPrompt instances, picks discovery vs
        #    practice based on the savestate files, and stops when the
        #    operator picks "end" on the prompt (or the design TSV is
        #    exhausted for practice).
        tasks_iter = phases.iter_tasks(
            config.paths,
            config.settings.task,
            make_discovery_task=make_discovery_task,
            make_practice_task=make_practice_task,
            make_prompt=make_prompt,
        )

        for task in tasks_iter:
            if isinstance(task, MarioTask):
                run_idx_counter[0] += 1
                # psychopy.logging.info takes a single string, unlike the
                # stdlib logger's printf-style API.
                logging.info(
                    f"Starting {task.name} with {len(task.state_names)} "
                    f"level(s): {task.state_names}"
                )
            task.setup(
                exp_win,
                output_path=config.paths.sourcedata_session_dir,
                output_fname_base=config.paths.session_prefix,
                use_eeg=use_eeg,
            )
            try:
                # Only gameplay runs sync: the end-of-run prompt is the
                # operator answering a question, not a run to align.
                shortcut = _run_task(
                    task,
                    exp_win,
                    use_eeg=use_eeg,
                    sync_obj=sync_obj if isinstance(task, MarioTask) else None,
                )
            finally:
                task.unload()

            if shortcut == "q":
                print("Session quit (Ctrl+Q).")
                return 130
            if shortcut == "c" and isinstance(task, MarioTask):
                # Ctrl+C aborted gameplay; phases.iter_tasks will still
                # yield the end-of-run prompt next, so the operator gets
                # to decide retry vs end.
                print(f"Run {run_idx_counter[0]} aborted (Ctrl+C).")
            if shortcut == "n" and isinstance(task, MarioTask):
                # There is no restart here yet: tasks come from a generator,
                # not a numbered loop, so "same run again" is not a `continue`.
                # Say so rather than letting it look like a silent Ctrl+C —
                # the end-of-run prompt is how you replay a level today.
                print(
                    f"Run {run_idx_counter[0]} ended (Ctrl+N). Restart is not "
                    f"implemented; use the end-of-run prompt to play again."
                )
        print(f"Session ended after {run_idx_counter[0]} run(s).")
        return 0
    finally:
        # Tear down in reverse order, never masking an in-flight exception.
        _remove_quit_key()
        try:
            exp_win.close()
        except Exception:  # noqa: BLE001
            pass
        sync_obj.close()
        markers.close()
        log_setup.flush()
