"""Session orchestrator — Phase 1 version.

This module owns:
    * the PsychoPy window lifetime
    * the PsychoPy LogFile lifetime
    * the EEG marker backend lifetime
    * the retro custom-path registration
    * the task lifecycle loop (setup → instructions → run → stop → save)

Phase 1 runs a single :class:`mario_task.task.MarioTask` covering
``state_names=["Level1-1"]`` for ``settings.task.max_duration_seconds``.
Phase 2 will replace the explicit ``MarioTask(...)`` construction with
``phases.iter_tasks(config)`` and add Ctrl+N restart handling.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psychopy.logging as psy_logging
import retro
from psychopy import core, event, logging, visual

from mario_task import design, log_setup, markers, phases
from mario_task.paths import BidsPaths, check_data_root
from mario_task.questionnaire import build_default_questions
from mario_task.settings import Settings
from mario_task.task import DEFAULT_KEY_SET, EndOfRunPrompt, MarioTask, _TaskBase


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


def _listen_shortcuts() -> str | None:
    """Return a single-char shortcut keypress or ``None``.

    Ctrl+C → ``"c"`` (abort current task)
    Ctrl+Q → ``"q"`` (quit the session)
    Ctrl+N → ``"n"`` (Phase 2: restart current task — ignored in Phase 1)
    """
    if any(k[1] & event.MOD_CTRL for k in event._keyBuffer):
        keys = event.getKeys(["n", "c", "q"], modifiers=True)
        ctrl = any(k[1]["ctrl"] for k in keys)
        names = [k[0] for k in keys]
        if names and ctrl:
            return names[0]
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
) -> str | None:
    """Drive one task through its lifecycle. Returns the shortcut that ended it."""
    print(f"Next task: {task}")
    shortcut = _run_task_loop(task.instructions(exp_win, None))

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
    )

    # 5. Register the retro custom path so it can find SuperMarioBros-Nes.
    #    Must happen before any retro.make(). retro caches paths and may
    #    re-resolve them at make() time, so we always pass an *absolute*
    #    path here rather than relying on the caller's cwd.
    retro.data.Integrations.add_custom_path(str(data_root.parent.resolve()))

    # 6. Open the PsychoPy window.
    exp_win = _build_window(config.settings)

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
        fixation_duration = float(config.settings.task.fixation_duration_seconds)
        max_duration = float(config.settings.task.max_duration_seconds)
        subject_q_tsv = config.paths.questionnaire_tsv if post_run_ratings else None

        def make_discovery_task(level_name: str, run_idx: int) -> MarioTask:
            return MarioTask(
                name=f"task-mario_phase-discovery_run-{run_idx + 1:02d}",
                state_names=[level_name],
                max_duration=max_duration,
                repeat_scenario=True,
                key_set=DEFAULT_KEY_SET,
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
                key_set=DEFAULT_KEY_SET,
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
                shortcut = _run_task(task, exp_win, use_eeg=use_eeg)
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
        print(f"Session ended after {run_idx_counter[0]} run(s).")
        return 0
    finally:
        exp_win.close()
        log_setup.flush()
