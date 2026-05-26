"""Task lifecycle base + the flattened ``MarioTask`` + ``Pause``.

Lifecycle (port of upstream ``task_base.py:Task``):

    setup(exp_win, output_path, output_fname_base, use_eeg) — pre-flight
    instructions(exp_win, ctl_win) — generator: yields one flip per frame
    run(exp_win, ctl_win) — generator: yields one flip per frame
    stop(exp_win, ctl_win) — generator: yields one flip per frame
    save() — write the events TSV

Subclasses override the protected variants (``_setup``, ``_instructions``,
``_run``, ``_stop``) — the public ones manage flips, EEG markers, and
the ``_task_completed`` flag.

The :class:`MarioTask` class is intentionally flat — it replaces the
upstream's ``VideoGameBase → VideoGame → VideoGameMultiLevel`` three-tier
inheritance with a single class that takes a ``state_names: list[str]``
and a ``repeat_scenario: bool``. Discovery and practice differ only in
those two arguments.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Generator

import numpy as np
import pandas as pd
import retro
from PIL import Image
from psychopy import constants, event, logging, visual

from mario_task import engine, markers, questionnaire
from mario_task.audio import SoundDeviceGameBlockStream
from mario_task.input import ControllerInput, install as install_input, uninstall as uninstall_input

if TYPE_CHECKING:
    from psychopy.visual import Window


# Default 12-button NES key mapping; matches upstream DEFAULT_KEY_SET.
DEFAULT_KEY_SET: list[str] = ["z", "_", "_", "_", "up", "down", "left", "right", "x", "_", "_", "_"]


# ---------------------------------------------------------------------------
# _TaskBase: the lifecycle scaffolding shared by all tasks
# ---------------------------------------------------------------------------


class _TaskBase:
    """Generator-per-frame lifecycle harness, ported from upstream ``Task``.

    Concrete tasks override the underscore-prefixed methods:
        ``_setup(exp_win)`` — one-shot init before any frames run.
        ``_instructions(exp_win, ctl_win)`` — yields a clearBuffer bool per frame.
        ``_run(exp_win, ctl_win)`` — yields a clearBuffer bool per frame.
        ``_stop(exp_win, ctl_win)`` — optional cleanup, yields per frame.

    Contract for subclasses:
        * Set ``self.duration`` in ``__init__`` to enable the tqdm progress bar.
        * Append ``self._events`` rows for the BIDS events TSV.
        * Set ``self._task_completed = True`` is done automatically by ``run``
          when its generator exits cleanly.
    """

    DEFAULT_INSTRUCTION = ""

    def __init__(self, name: str, instruction: str | None = None) -> None:
        self.name = name
        self.instruction = self.DEFAULT_INSTRUCTION if instruction is None else instruction
        self._task_completed = False
        # ``flags`` is a per-task state bitmask used by the EEG marker
        # callback. ``flags & 4`` signals "currently in gameplay; suppress
        # per-flip markers (we push per emulator-step instead)".
        self.flags = 0

    # ----- public lifecycle (don't override in subclasses) -----

    def setup(
        self,
        exp_win: "Window",
        output_path: str | Path,
        output_fname_base: str,
        *,
        use_eeg: bool = False,
    ) -> None:
        self.output_path = str(output_path)
        self.output_fname_base = output_fname_base
        self.use_eeg = use_eeg
        self._events: list[dict] = []
        self._exp_win_first_flip_time: float | None = None
        self._exp_win_last_flip_time: float | None = None
        self._ctl_win_last_flip_time: float | None = None
        self._setup(exp_win)

    def _setup(self, exp_win: "Window") -> None:  # noqa: D401, ARG002
        """Override in subclasses."""

    def _flip_all_windows(
        self, exp_win: "Window", ctl_win: "Window | None" = None, clearBuffer: bool = True
    ) -> None:
        if ctl_win is not None:
            ctl_win.timeOnFlip(self, "_ctl_win_last_flip_time")
            ctl_win.flip(clearBuffer=clearBuffer)
        exp_win.flip(clearBuffer=clearBuffer)
        exp_win.timeOnFlip(self, "_exp_win_last_flip_time")

    def instructions(self, exp_win: "Window", ctl_win: "Window | None") -> Generator[None, None, None]:
        if hasattr(self, "_instructions"):
            for clearBuffer in self._instructions(exp_win, ctl_win):
                yield
                self._flip_all_windows(exp_win, ctl_win, clearBuffer)
        # Two clear flips to wipe the back buffer cleanly.
        for _ in range(2):
            yield
            self._flip_all_windows(exp_win, ctl_win, True)

    def run(self, exp_win: "Window", ctl_win: "Window | None") -> Generator[None, None, None]:
        # First flip syncs the task clock to wall time.
        exp_win.timeOnFlip(self, "_exp_win_first_flip_time")
        self._flip_all_windows(exp_win, ctl_win, True)

        from psychopy import core

        self.task_timer = core.MonotonicClock(self._exp_win_first_flip_time)

        flip_idx = 0
        for clearBuffer in self._run(exp_win, ctl_win):
            yield
            if self.use_eeg and markers.EEG_MARKERS_ON_FLIP:
                marker = self._eeg_marker_value(flip_idx)
                if marker is not None:
                    exp_win.callOnFlip(markers.send_signal, marker)
            self._flip_all_windows(exp_win, ctl_win, clearBuffer)
            flip_idx += 1
        self._task_completed = True

    def stop(self, exp_win: "Window", ctl_win: "Window | None") -> Generator[None, None, None]:
        if hasattr(self, "_stop"):
            for clearBuffer in self._stop(exp_win, ctl_win):
                yield
                self._flip_all_windows(exp_win, ctl_win, clearBuffer)
        for _ in range(2):
            self._flip_all_windows(exp_win, ctl_win, True)

    def _eeg_marker_value(self, flip_idx: int) -> int | None:  # noqa: ARG002
        """Marker value pushed on each non-gameplay flip. ``None`` = skip.

        Default: ``NON_GAME_FLIP`` (3). VideoGame-style tasks return
        ``None`` while gameplay is active (``flags & 4``) so we don't
        double-mark — the per-emulator-step marker handles it.
        """
        return markers.NON_GAME_FLIP

    def _log_event(self, ev: dict, clock: str = "task") -> None:
        if clock == "task":
            onset = self.task_timer.getTime()
        elif clock == "flip":
            assert self._exp_win_first_flip_time is not None
            assert self._exp_win_last_flip_time is not None
            onset = self._exp_win_last_flip_time - self._exp_win_first_flip_time
        else:
            raise ValueError(f"unknown clock {clock!r}")
        ev.update({"onset": onset, "sample": time.monotonic()})
        self._events.append(ev)

    def save(self) -> None:
        """Write the per-task events TSV. No-op if no events were recorded."""
        if not self._events:
            return
        fname = self._generate_unique_filename("events", "tsv")
        pd.DataFrame(self._events).to_csv(fname, sep="\t", index=False)
        logging.exp(f"Saved {len(self._events)} events to {fname}")

    def unload(self) -> None:
        """Override for tasks that hold expensive resources (emulator, etc.)."""

    def _generate_unique_filename(self, suffix: str, ext: str = "tsv") -> str:
        base = os.path.join(self.output_path, f"{self.output_fname_base}_{self.name}_{suffix}.{ext}")
        if not os.path.exists(base):
            return base
        fi = 1
        while True:
            candidate = os.path.join(
                self.output_path,
                f"{self.output_fname_base}_{self.name}_{suffix}-{fi:03d}.{ext}",
            )
            if not os.path.exists(candidate):
                return candidate
            fi += 1


# ---------------------------------------------------------------------------
# MarioTask — flattened gameplay task
# ---------------------------------------------------------------------------


class MarioTask(_TaskBase):
    """A single Mario "run".

    Plays through ``state_names`` in order, replaying the playlist
    until ``max_duration`` is reached if ``repeat_scenario=True``.
    Used for both discovery (single level repeated for the full run)
    and practice (22 different levels cycled).

    Public attribute contracts (read by :mod:`mario_task.phases`):
        ``_completed``: True iff the most recent emulator episode ended
            with ``lives > -1`` (i.e. not a game over).
        ``_task_completed``: True iff ``run()`` exited cleanly.
        ``_nlevels``: number of distinct levels played during this run.
        ``_events``: BIDS events TSV rows, auto-saved by ``save()``.
    """

    DEFAULT_INSTRUCTION = (
        "Super Mario Bros — {state_name}\n\n"
        "Controls:\n"
        "  Arrow keys: move\n"
        "  Z: run / fire\n"
        "  X: jump\n\n"
        "Press X when you’re ready to start."
    )
    READY_KEY = "x"
    """Keyboard key the subject presses to start the run. Read by ``_instructions``."""

    def __init__(
        self,
        *,
        name: str,
        state_names: list[str],
        max_duration: float,
        repeat_scenario: bool = True,
        key_set: list[str] | None = None,
        post_run_ratings: list[tuple[int, str, int]] | None = None,
        questionnaire_subject_tsv: str | os.PathLike[str] | None = None,
        questionnaire_subject_label: str | None = None,
        questionnaire_session_label: str | None = None,
        questionnaire_run_idx: int | None = None,
        fixation_duration: float = 2.0,
        game_name: str = "SuperMarioBros-Nes",
        scenario: str = "scenario",
        inttype: object = retro.data.Integrations.CUSTOM_ONLY,
        instruction: str | None = None,
        instruction_seconds: float = 600.0,
        bg_color: tuple[int, int, int] = (0, 0, 0),
        scaling: float = 1.0,
    ) -> None:
        super().__init__(name=name, instruction=instruction)
        if not state_names:
            raise ValueError("state_names must contain at least one level")
        self.state_names = list(state_names)
        self.max_duration = float(max_duration)
        self.duration = float(max_duration)  # surfaced for progress-bar consumers
        self.repeat_scenario = bool(repeat_scenario)
        self.key_set = list(key_set) if key_set is not None else list(DEFAULT_KEY_SET)
        self.post_run_ratings = post_run_ratings
        self.questionnaire_subject_tsv = (
            Path(questionnaire_subject_tsv) if questionnaire_subject_tsv else None
        )
        self.questionnaire_subject_label = questionnaire_subject_label
        self.questionnaire_session_label = questionnaire_session_label
        self.questionnaire_run_idx = questionnaire_run_idx
        self.fixation_duration = float(fixation_duration)
        self.instruction_seconds = float(instruction_seconds)
        self.game_name = game_name
        self.scenario = scenario
        self.inttype = inttype
        self._bg_color = bg_color
        self._scaling = scaling

        # Mutable runtime state, populated by setup() / run().
        self.state_name: str = self.state_names[0]
        self._completed = False
        self._nlevels = 0
        self._emulator_frame = 0
        self._game_info: dict = {}
        self.movie_path: str = ""
        self._rep_event: dict = {}

        # set by _setup
        self.emulator = None
        self.game_sound: SoundDeviceGameBlockStream | None = None
        self.game_vis_stim: visual.ImageStim | None = None
        self.game_fps: float = 60.0
        self._frameInterval: float = 1 / 60
        self._retraceInterval: float = 1 / 60
        self._first_frame = None
        self.input: ControllerInput = ControllerInput()

    # ----- setup -----

    def _setup(self, exp_win: "Window") -> None:
        self._retraceInterval = engine.detect_frame_interval(exp_win)

        self.emulator = engine.build_emulator(
            game_name=self.game_name,
            state_name=self.state_names[0],
            scenario=self.scenario,
            inttype=self.inttype,
            win_handle=exp_win.winHandle,
        )

        # Probe frame rate + audio block size by resetting once. The first
        # frame is rendered out of this reset; subsequent resets happen
        # inside the play loop.
        reset_out = self.emulator.reset()
        self._first_frame = reset_out[0] if isinstance(reset_out, tuple) else reset_out
        # SDL stole the GL context — restore PsychoPy's before allocating stimuli.
        exp_win.winHandle.switch_to()

        first_sound_chunk = self.emulator.em.get_audio()
        block_size = first_sound_chunk.shape[0]
        audio_rate = self.emulator.em.get_audio_rate()
        logging.exp(
            f"MarioTask: audio sample rate {audio_rate}, blocksize {block_size}"
        )
        self.game_sound = SoundDeviceGameBlockStream(
            sample_rate=audio_rate, block_size=0, dtype=np.int16
        )

        # Compute the on-screen image rectangle so the emulator frame
        # fits the window while preserving aspect.
        min_ratio = min(
            exp_win.size[0] / self._first_frame.shape[1],
            exp_win.size[1] / self._first_frame.shape[0],
        )
        width = int(min_ratio * self._first_frame.shape[1] * self._scaling)
        height = int(min_ratio * self._first_frame.shape[0] * self._scaling)
        self.game_vis_stim = visual.ImageStim(
            exp_win,
            size=(width, height),
            units="pix",
            interpolate=False,
            flipVert=True,
            autoLog=False,
        )

        self.game_fps = self.emulator.em.get_screen_rate()
        self._frameInterval = 1.0 / self.game_fps

        # bk2 recording is started per-attempt inside _run (so the rep-NN
        # counter starts at 01 and increments cleanly). Setup's
        # emulator.reset() above is purely for ImageStim sizing.
        #
        # Our pyglet key hook is installed at the start of _run (not here)
        # so the instructions screen — which runs BEFORE _run — can still
        # use psychopy's default event.getKeys() to wait for the subject's
        # "ready" press. The hook is uninstalled in _stop.

    def _set_recording_file(self) -> None:
        """Choose a unique bk2 path and start recording.

        Naming: ``<prefix>_<task_name>_<state_name>_rep-<NN>.bk2`` where
        ``NN`` is the 1-indexed attempt counter within the current run
        (``self._nlevels``). The emulator game name is intentionally
        absent — the repo only handles SuperMarioBros-Nes, and including
        it in every filename was redundant noise.

        On the rare collision (e.g. a previous session crashed and left
        a bk2 with the same rep number, then datalad rewinds time on a
        re-run) we bump a uniqueness suffix.
        """
        base = os.path.join(
            self.output_path,
            f"{self.output_fname_base}_{self.name}_{self.state_name}_rep-{self._nlevels:02d}.bk2",
        )
        candidate = base
        nnn = 0
        while os.path.exists(candidate):
            nnn += 1
            candidate = base.replace(".bk2", f"-{nnn:03d}.bk2")
        self.movie_path = candidate
        logging.exp(f"MarioTask: recording bk2 to {self.movie_path}")
        self.emulator.record_movie(self.movie_path)

    # ----- runtime helpers used by engine.run_emulator -----

    def _render_graphics_sound(self, obs, sound_block, exp_win, ctl_win) -> None:
        # Pillow path avoids a redundant uint8→float→uint8 conversion psychopy
        # would otherwise do for the obs ndarray.
        self.game_vis_stim.image = Image.fromarray(obs).transpose(Image.FLIP_TOP_BOTTOM)
        self.game_vis_stim.draw(exp_win)
        if ctl_win is not None:
            self.game_vis_stim.draw(ctl_win)
        self.game_sound.put(sound_block)
        if self.game_sound.status != constants.PLAYING:
            exp_win.callOnFlip(self.game_sound.play)  # start sound only at flip

    def _eeg_marker_value(self, flip_idx: int) -> int | None:
        # During gameplay, suppress per-flip markers (we push per
        # emulator-step inside engine.run_emulator instead).
        if self.flags & 4:
            return None
        return super()._eeg_marker_value(flip_idx)

    # ----- the main run loop -----

    def _instructions(self, exp_win: "Window", ctl_win: "Window | None") -> Generator[bool, None, None]:
        """Hold the instructions screen until the subject presses ``READY_KEY``.

        Uses psychopy's ``event.getKeys`` (not our pyglet hook), which is
        why our hook is installed at the start of ``_run`` and not in
        ``_setup``. A safety timeout (``instruction_seconds``) bounds the
        wait so a stuck subject doesn't strand the experiment — set it
        very large (default 600 s) for an effectively unlimited wait.
        """
        text = self.instruction.format(game_name=self.game_name, state_name=self.state_name)
        win_w, win_h = exp_win.size
        # Scale text with screen height: ~3.5% of height, floor at 24 px so
        # the instructions are still readable on small dev windows.
        text_height = max(24, win_h // 28)
        stim = visual.TextStim(
            exp_win,
            text=text,
            units="pix",
            height=text_height,
            wrapWidth=int(win_w * 0.75),
            color="white",
            alignText="center",
            anchorHoriz="center",
            anchorVert="center",
        )

        # Flush any stale presses (e.g. X held during the previous
        # questionnaire submission) so we don't auto-advance.
        event.clearEvents()

        max_frames = max(60, int(self.instruction_seconds / self._retraceInterval))
        for frame_n in range(max_frames):
            stim.draw(exp_win)
            if ctl_win is not None:
                stim.draw(ctl_win)
            yield frame_n < 2  # clear back-buffer on the first couple of flips only
            if event.getKeys([self.READY_KEY]):
                return
        # Safety timeout hit; advance anyway (operator can Ctrl+Q if stuck).
        yield True

    def _fixation(self, exp_win: "Window", ctl_win: "Window | None") -> Generator[bool, None, None]:
        """Show a centred ``+`` for ``self.fixation_duration`` seconds.

        Logged once as a ``fixation`` event (single onset + duration row).
        Per-flip EEG markers use the default ``NON_GAME_FLIP`` value
        because ``flags`` is reset to 0 by the engine on exit.
        """
        if self.fixation_duration <= 0:
            return
        win_h = exp_win.size[1]
        cross_size = max(40, win_h // 10)
        stim = visual.TextStim(
            exp_win,
            text="+",
            units="pix",
            height=cross_size,
            color="white",
            anchorHoriz="center",
            anchorVert="center",
        )
        self._log_event({"trial_type": "fixation", "duration": self.fixation_duration})
        n_frames = max(1, int(self.fixation_duration / self._retraceInterval))
        for _ in range(n_frames):
            stim.draw(exp_win)
            if ctl_win is not None:
                stim.draw(ctl_win)
            yield True

    def _run(self, exp_win: "Window", ctl_win: "Window | None") -> Generator[bool, None, None]:
        # Outer loop: cycle through state_names, restarting the playlist if
        # repeat_scenario is True. Inner loop: each state_name's gameplay
        # delegates to engine.run_emulator. A fixation cross is shown
        # before every attempt, and post_run_ratings (if any) fire once
        # at the end before _run returns.
        exp_win.setColor(self._bg_color, colorSpace="rgb255")
        if ctl_win is not None:
            ctl_win.setColor(self._bg_color, colorSpace="rgb255")
        for _ in range(2):  # warm-up flips so bg color takes hold
            yield True

        # Install the pyglet key hook now (not in _setup) so that the
        # instructions screen, which runs BEFORE _run, can still use
        # psychopy's default event.getKeys() to wait for the subject's
        # "ready" press. Uninstalled in _stop.
        install_input(exp_win)

        self._nlevels = 0
        while True:
            for level in self.state_names:
                self.state_name = level
                self.emulator.load_state(level, inttype=self.inttype)
                self.emulator.data.load(
                    retro.data.get_file_path(self.game_name, "data.json", inttype=self.inttype),
                    retro.data.get_file_path(self.game_name, f"{self.scenario}.json", inttype=self.inttype),
                )

                # Always open a fresh bk2 per attempt. _nlevels is the
                # 1-indexed within-run repetition counter that drives the
                # rep-NN suffix in the filename.
                self._nlevels += 1
                self._set_recording_file()
                reset_out = self.emulator.reset()
                self._first_frame = (
                    reset_out[0] if isinstance(reset_out, tuple) else reset_out
                )

                # Drain pyglet event buffers before each attempt so
                # buttons held during the previous attempt don't bleed
                # over.
                self.input = ControllerInput()
                exp_win.winHandle.dispatch_events()

                # Brief fixation cross between attempts (also before the
                # very first attempt of the run).
                yield from self._fixation(exp_win, ctl_win)

                yield from engine.run_emulator(self, exp_win, ctl_win)

                # After each attempt, check the run-level time budget.
                if self.max_duration and self.task_timer.getTime() > self.max_duration:
                    yield from self._post_run(exp_win, ctl_win)
                    return
            if not self.repeat_scenario:
                yield from self._post_run(exp_win, ctl_win)
                return

    def _post_run(self, exp_win: "Window", ctl_win: "Window | None") -> Generator[bool, None, None]:
        """Post-gameplay UI: optional Likert questionnaire.

        Called from explicit exit points in :meth:`_run` so a Ctrl+C
        abort (which closes the generator with ``GeneratorExit``) cleanly
        skips the ratings step. ``yield from`` is illegal in a
        ``finally`` block during ``close()``, hence the explicit calls.
        """
        if self.post_run_ratings:
            yield from questionnaire.run(
                self,
                exp_win,
                ctl_win,
                self.post_run_ratings,
                subject_tsv_path=self.questionnaire_subject_tsv,
                subject=self.questionnaire_subject_label,
                session=self.questionnaire_session_label,
                run_idx=self.questionnaire_run_idx,
            )

    def _stop(self, exp_win: "Window", ctl_win: "Window | None") -> Generator[bool, None, None]:
        if self.game_sound is not None:
            self.game_sound.stop()
        if self.emulator is not None:
            self.emulator.stop_record()
        # Flush any keypress events left in input buffer to events TSV.
        self._events.extend(self.input.drain_events())
        uninstall_input(exp_win)
        exp_win.setColor([0] * 3, colorSpace="rgb")
        if ctl_win is not None:
            ctl_win.setColor([0] * 3, colorSpace="rgb")
        yield True

    def unload(self) -> None:
        if self.emulator is not None:
            self.emulator.close()
            self.emulator = None


# ---------------------------------------------------------------------------
# Pause — text screen that waits for a key
# ---------------------------------------------------------------------------


class Pause(_TaskBase):
    """Inter-run pause screen. Waits for ``wait_key`` (psychopy key name)."""

    def __init__(
        self,
        text: str = "Taking a short break, relax...",
        *,
        wait_key: str | bool = False,
        name: str = "Pause",
    ) -> None:
        super().__init__(name=name)
        self.text = text
        self.wait_key = wait_key

    def _run(self, exp_win: "Window", ctl_win: "Window | None") -> Generator[bool, None, None]:
        win_w, win_h = exp_win.size
        stim = visual.TextStim(
            exp_win,
            text=self.text,
            units="pix",
            height=max(24, win_h // 28),
            wrapWidth=int(win_w * 0.75),
            color="white",
            alignText="center",
            anchorHoriz="center",
            anchorVert="center",
        )
        while True:
            if self.wait_key is not False:
                if event.getKeys([self.wait_key]):  # type: ignore[list-item]
                    return
            stim.draw(exp_win)
            if ctl_win is not None:
                stim.draw(ctl_win)
            yield True


class EndOfRunPrompt(_TaskBase):
    """End-of-run UI: choose to start another run or end the session.

    Renders a centred multi-line text screen and waits for one of two
    keys. Sets :attr:`pressed` to ``"continue"`` or ``"end"`` once a key
    is detected, so the parent session loop can decide what to do.

    Uses psychopy's ``event.getKeys`` (not the gameplay input handler)
    because gameplay input was uninstalled at the previous task's
    ``_stop``. The two keys default to ``x`` (continue, the A/jump
    button) and ``z`` (end, the B/run button).
    """

    DEFAULT_TEMPLATE = (
        "Run {run_idx} complete.\n\n"
        "Take a short break.\n\n"
        "Press {continue_key_label} when you’re ready for the next run.\n"
        "Press {end_key_label} to end the session."
    )

    def __init__(
        self,
        *,
        run_idx: int,
        continue_key: str = "x",
        end_key: str = "z",
        text_template: str | None = None,
        name: str = "EndOfRunPrompt",
    ) -> None:
        super().__init__(name=name)
        if continue_key.lower() == end_key.lower():
            raise ValueError("continue_key and end_key must differ")
        self.run_idx = int(run_idx)
        self.continue_key = continue_key
        self.end_key = end_key
        template = text_template if text_template is not None else self.DEFAULT_TEMPLATE
        self.text = template.format(
            run_idx=self.run_idx,
            continue_key_label=self.continue_key.upper(),
            end_key_label=self.end_key.upper(),
        )
        self.pressed: str | None = None

    def _run(self, exp_win: "Window", ctl_win: "Window | None") -> Generator[bool, None, None]:
        win_w, win_h = exp_win.size
        stim = visual.TextStim(
            exp_win,
            text=self.text,
            units="pix",
            height=max(28, win_h // 26),
            wrapWidth=int(win_w * 0.75),
            color="white",
            alignText="center",
            anchorHoriz="center",
            anchorVert="center",
        )
        # Flush any stale key events left over from gameplay/questionnaire
        # so a Z held during questionnaire submission doesn't immediately
        # end the session here.
        event.clearEvents()
        while True:
            keys = event.getKeys([self.continue_key, self.end_key])
            if keys:
                self.pressed = "continue" if self.continue_key in keys else "end"
                self._log_event({
                    "trial_type": "end_of_run_prompt",
                    "run_idx": self.run_idx,
                    "choice": self.pressed,
                    "key": keys[0],
                })
                return
            stim.draw(exp_win)
            if ctl_win is not None:
                stim.draw(ctl_win)
            yield True
