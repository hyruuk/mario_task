"""retro + psychopy frame loop.

Two responsibilities:

1. :func:`build_emulator` — construct the ``retro`` env, wrap its native
   methods so they restore PsychoPy's OpenGL context on return (stable-retro's
   SDL backend hijacks the current context on every call, leaving the
   PsychoPy window unable to draw), and return both the emulator and the
   wrapped frame-rate info.

2. :func:`run_emulator` — the actual per-frame generator: pace to the
   emulator's frame rate, step the emulator, push EEG markers per step,
   render the obs into a psychopy ImageStim, push audio into the
   sounddevice queue, and yield once per frame so the parent task's
   ``run()`` loop can flip the windows.

Both are essentially verbatim ports of upstream ``videogame.py`` lines
278-325 (setup) and 380-491 (loop). The frame pacing, dropped-frame
handling, GL context wrap and per-step EEG markers are load-bearing —
do not refactor without re-validating bk2/events/marker timing.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Generator

import retro
from psychopy import logging

from mario_task import markers
from mario_task.audio import SoundDeviceGameBlockStream

if TYPE_CHECKING:
    from psychopy.visual import Window

    from mario_task.task import MarioTask


# Methods on the retro emulator that hijack the OpenGL current context.
# Each must be wrapped to call winHandle.switch_to() after the original
# function returns; otherwise the PsychoPy window goes black on first
# emulator.reset(). The ``data.load`` method is wrapped separately because
# it lives on the emulator's ``data`` sub-object.
_EMULATOR_GL_HIJACK_METHODS = (
    "reset",
    "step",
    "load_state",
    "record_movie",
    "stop_record",
    "close",
)


def _wrap_for_gl_context(orig, win_handle):
    """Wrap an emulator method so it restores PsychoPy's GL context on return."""

    def wrapped(*args, **kwargs):
        out = orig(*args, **kwargs)
        win_handle.switch_to()
        return out

    return wrapped


def build_emulator(
    *,
    game_name: str,
    state_name: str | None,
    scenario: str | None,
    inttype: Any = retro.data.Integrations.CUSTOM_ONLY,
    win_handle,
):
    """Construct a stable-retro emulator and patch it for PsychoPy GL ownership.

    Returns the emulator object. The caller is responsible for calling
    ``emulator.close()`` on teardown (or just dropping the reference —
    retro's destructor handles it).
    """
    emulator = retro.make(
        game_name,
        state=state_name,
        scenario=scenario,
        record=False,
        inttype=inttype,
        # stable-retro's default render_mode="human" opens its own SDL
        # window showing the emulator framebuffer. We render obs ourselves
        # into a psychopy ImageStim, so suppress retro's window entirely.
        render_mode=None,
    )
    for method_name in _EMULATOR_GL_HIJACK_METHODS:
        orig = getattr(emulator, method_name, None)
        if orig is None:
            continue
        try:
            setattr(emulator, method_name, _wrap_for_gl_context(orig, win_handle))
        except (AttributeError, TypeError):
            # Some methods on the C extension may be read-only.
            pass
    # data.load lives on a sub-object; wrap it separately.
    try:
        emulator.data.load = _wrap_for_gl_context(emulator.data.load, win_handle)
    except (AttributeError, TypeError):
        pass
    return emulator


def detect_frame_interval(exp_win: "Window") -> float:
    """Return ``1.0 / monitor_refresh_rate``.

    Falls back to 60 Hz with a warning if PsychoPy can't determine the
    refresh rate — unlike upstream which silently used 60.0, we log so
    the operator notices.
    """
    rate = exp_win._monitorFrameRate
    if rate is None:
        rate = exp_win.getActualFrameRate()
    if rate is None:
        logging.warning(
            "Monitor frame rate could not be determined; defaulting to 60 Hz."
        )
        rate = 60.0
    return 1.0 / rate


def run_emulator(
    task: "MarioTask",
    exp_win: "Window",
    ctl_win: "Window | None",
) -> Generator[bool, None, None]:
    """One full gameplay "attempt" — runs until retro signals ``_done``.

    Yields ``False`` on every frame so the parent ``Task.run`` loop knows
    not to clear the back-buffer between flips (we draw the emulator
    frame fresh each time).

    Side effects on ``task`` (the contract documented in MarioTask):

    * Sets ``task._completed = True`` iff the player ended with lives > -1
      (i.e. did not game-over).
    * Sets ``task._emulator_frame`` to the current emulator step index
      throughout (used by the EEG-marker callback during flips).
    * Appends a single ``gym-retro_game`` row to ``task._events`` (BIDS
      events TSV) capturing the duration, nframes, and movie path of
      this attempt.

    Per-step EEG markers (one per ``emulator.step()``, not per render
    flip) are emitted so the marker count exactly matches the bk2
    frame count even when render frames are dropped.
    """
    # Render the initial frame and prime audio.
    task._render_graphics_sound(
        task._first_frame, task.emulator.em.get_audio(), exp_win, ctl_win
    )

    # task.flags = 4 tells _eeg_marker_value() to suppress per-flip markers
    # during gameplay (we push per emulator-step instead).
    task.flags = 4

    # Initial event row; nframes/offset/duration filled in at the end.
    task._rep_event = {
        "trial_type": "gym-retro_game",
        "game": task.game_name,
        "level": task.state_name,
        "stim_file": task.movie_path,
        "onset": task.task_timer.getTime(),
    }
    task._events.append(task._rep_event)

    # Mark the gameplay segment in the EEG stream so analysts can find
    # the start of each "reset → done" episode in the bk2 movie.
    if task.use_eeg and markers.EEG_MARKERS_ON_FLIP:
        markers.send_signal(markers.GAME_RESET, timestamp=markers.now())

    # Log the bk2 "open" line so a downstream verifier can pair it with
    # the bk2 file on disk and check that the frame count matches.
    # Format is parsed by mario_task.verify so keep it stable.
    logging.exp(
        f"bk2_start path={task.movie_path} state={task.state_name} frame=1 "
        f"timer={task.task_timer.getTime():.6f}"
    )

    _done = False
    _nextFrameT = task.task_timer.getTime()
    level_step = 0
    total_reward = 0.0
    # Track jump_airborne to detect the flag-pole grab (rising edge to 3).
    # Same logic as the upstream `generate_replays.py` outcome detector:
    # jump_airborne transitions 0/1/2 → 3 when Mario grabs the flag = level
    # cleared. Initialize to a sentinel that can't equal 3 so the first
    # frame doesn't trigger spuriously.
    prev_jump_airborne = 0
    yield False  # let the run() loop perform the first flip

    while not _done:
        level_step += 1
        task._emulator_frame = level_step
        _nextFrameT += task._frameInterval

        # Read the held-button snapshot the input subsystem maintains and
        # advance the emulator one frame.
        task.input.poll(exp_win, task.task_timer)
        keys = task.input.held_for(task.key_set)
        step_out = task.emulator.step(keys)

        # bk2 records exactly one frame per emulator.step(); push the
        # corresponding marker here (not in the per-flip callback) so
        # the marker count matches the bk2 even when render frames are
        # dropped below. Capture the LSL clock *immediately* after step()
        # so the sample is stamped with the engine's frame-advance time
        # rather than the (later) push time.
        #
        # Decimation: triggers.trigger_every=N emits one trigger per N
        # frames (f1, f1+N, f1+2N, ...). The cycling byte value
        # (codes.game_frame_mod) advances per *sent* trigger, not per
        # emulator frame. The .log line below records the bk2-frame index
        # of every sent trigger so analysts can re-align after the fact.
        trigger_every = markers.get_trigger_every()
        if (
            task.use_eeg
            and markers.EEG_MARKERS_ON_FLIP
            and (level_step - 1) % trigger_every == 0
        ):
            trigger_idx = (level_step - 1) // trigger_every
            value = markers.encode_frame(trigger_idx)
            markers.send_signal(value, timestamp=markers.now())
            logging.exp(
                f"trigger_sent frame={level_step} trigger_idx={trigger_idx} "
                f"value={value} trigger_every={trigger_every}"
            )

        # gymnasium returns 5-tuples (obs, rew, terminated, truncated, info);
        # legacy gym returns 4-tuples (obs, rew, done, info).
        if len(step_out) == 5:
            _obs, _rew, _terminated, _truncated, task._game_info = step_out
            _done = _terminated or _truncated
        else:
            _obs, _rew, _done, task._game_info = step_out

        total_reward += _rew

        # Level-complete detection: flag pole grab transitions jump_airborne
        # from anything ≠ 3 to 3. We OR into task._completed so it stays True
        # once set (carries over across multiple replays in the same run,
        # which is what the discovery savestate advancement reads at the
        # end). Falls back gracefully if jump_airborne isn't exposed.
        cur_jump_airborne = task._game_info.get("jump_airborne", -1)
        if cur_jump_airborne == 3 and prev_jump_airborne != 3:
            task._completed = True
            exp_win.logOnFlip(
                level=logging.EXP,
                msg=f"Level cleared at step {level_step}: {task.state_name}",
            )
            task._log_event({
                "trial_type": "level_complete",
                "level": task.state_name,
                "nframes": level_step,
            })
        prev_jump_airborne = cur_jump_airborne

        # If we're already behind the next emulator frame's display
        # deadline, drop the render but keep the audio so the bk2 / event /
        # marker counts stay aligned with emulator steps.
        if _nextFrameT < task.task_timer.getTime():
            logging.warning(f"frame {level_step} dropped before render")
            task.game_sound.put(task.emulator.em.get_audio())
            continue

        task._render_graphics_sound(
            _obs, task.emulator.em.get_audio(), exp_win, ctl_win
        )

        if _done:
            exp_win.logOnFlip(
                level=logging.EXP,
                msg=f"VideoGame {task.state_name} stopped at {time.time():f}",
            )
        # Per-frame timestamp log so a downstream verifier can compare the
        # bk2 frame count against the log without parsing every per-flip
        # message. Format kept stable (frame=N timer=T) — see verify.py.
        exp_win.logOnFlip(
            level=logging.EXP, msg=f"bk2_frame frame={level_step}"
        )

        # Spin-poll until the display deadline approaches; this is how
        # upstream pins emulator FPS to monitor refresh without using a
        # blocking sleep that would prevent pyglet events from being
        # processed.
        while _nextFrameT > (task.task_timer.getTime() + task._retraceInterval * 0.9):
            time.sleep(0.0001)
            exp_win.winHandle.dispatch_events()
            if ctl_win is not None:
                ctl_win.winHandle.dispatch_events()

        if _nextFrameT < task.task_timer.getTime():
            logging.warning(f"frame {level_step} dropped")
            continue

        yield False  # ready for flip; do not clear back-buffer

    # End of gameplay episode.
    task.flags = 0
    task._emulator_frame = 0
    task._rep_event["nframes"] = level_step
    task._rep_event["offset"] = task.task_timer.getTime()
    task._rep_event["duration"] = task._rep_event["offset"] - task._rep_event["onset"]
    # task._completed was already set to True if the player hit the flag
    # pole during this episode (see jump_airborne == 3 check above). It
    # stays sticky across replays within a run so phases.py only advances
    # the discovery savestate when the level was actually cleared.
    logging.exp(
        f"bk2_end path={task.movie_path} state={task.state_name} "
        f"frame={level_step} total_frames={level_step} "
        f"timer={task.task_timer.getTime():.6f} completed={task._completed}"
    )
    task.game_sound.flush()
