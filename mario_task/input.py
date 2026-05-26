"""Pyglet keypress / release interleaver.

Why a custom layer instead of using psychopy.event directly?

stable-retro's emulator expects a snapshot of "which buttons are held
down right now" on every frame. PsychoPy's ``event.getKeys`` returns
keys pressed *since the last call*, which doesn't tell us about held
keys, and the underlying pyglet key handlers default to capturing
each event separately. We need to:

1. Receive every key press AND release event with a timestamp.
2. Merge them by timestamp before draining — otherwise a tap whose
   release lands in the same dispatch_events() cycle as the press can
   have its release processed first, leaving the key "stuck pressed".
3. Maintain a snapshot dict (``pressed_keys``) of currently held keys
   that the gameplay loop reads on every emulator step.

This module owns the two module-level buffers (``_keyPressBuffer`` and
``_keyReleaseBuffer``) that pyglet writes into via the handlers we
install on the PsychoPy window. ``ControllerInput`` instances drain
those buffers, merge by timestamp, and maintain their own
``pressed_keys`` dict + a list of "just pressed" events for the frame.

Ported essentially verbatim from upstream ``videogame.py:343-377`` —
do not refactor the merge logic without understanding the bug it
fixes (see comment in :meth:`ControllerInput.poll`).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pyglet
from psychopy import core, event, logging

if TYPE_CHECKING:
    from psychopy.visual import Window


# Module-level buffers populated by the pyglet callbacks (which are installed
# on the window handle by `install`). These are intentionally module-globals
# because pyglet's hooks are functions, not bound methods.
_keyPressBuffer: list[tuple[str, float]] = []
_keyReleaseBuffer: list[tuple[str, float]] = []


def _on_pyglet_key_press(symbol: int, modifier: int) -> None:
    # Let psychopy's own key handler see the event (so its event.getKeys
    # still works for shortcut detection like Ctrl+Q).
    if modifier:
        event._onPygletKey(symbol, modifier)
    key = _normalize_key(symbol)
    _keyPressBuffer.append((key, core.getTime()))


def _on_pyglet_key_release(symbol: int, modifier: int) -> None:
    key = _normalize_key(symbol)
    logging.data(f"Keyrelease: {key}")
    _keyReleaseBuffer.append((key, core.getTime()))


def _normalize_key(symbol: int) -> str:
    """Convert a pyglet symbol to the lowercase string the key_set uses.

    e.g. ``pyglet.window.key.UP`` → ``"up"``, ``pyglet.window.key.X`` → ``"x"``.
    The lstrip removes the leading underscore pyglet uses for digits
    (``_1`` → ``1``) and the ``NUM_`` prefix on the numpad.
    """
    return pyglet.window.key.symbol_string(symbol).lower().lstrip("_").lstrip("NUM_")


def install(exp_win: "Window") -> None:
    """Install the pyglet key hooks on the experiment window.

    Call once during task setup. :func:`uninstall` restores PsychoPy's
    default handlers when gameplay is over.
    """
    exp_win.winHandle.on_key_press = _on_pyglet_key_press
    exp_win.winHandle.on_key_release = _on_pyglet_key_release


def uninstall(exp_win: "Window") -> None:
    """Restore PsychoPy's default key handler. Idempotent."""
    exp_win.winHandle.on_key_press = event._onPygletKey


class ControllerInput:
    """Per-task input state. Owns the ``pressed_keys`` snapshot and event log.

    Usage::

        ci = ControllerInput()
        install(exp_win)
        # ... each frame:
        ci.poll(exp_win, task_timer)
        keys = ci.held_for(key_set)   # list[bool], one per key_set entry
        # ... at end:
        events = ci.drain_events()    # list[dict] for the BIDS events TSV
        uninstall(exp_win)
    """

    def __init__(self) -> None:
        self.pressed_keys: dict[str, tuple[str, float]] = {}
        self._new_key_pressed: list[tuple[str, float]] = []
        self._events: list[dict] = []

    def poll(self, exp_win: "Window", task_timer: core.MonotonicClock) -> None:
        """Drain pyglet's press/release buffers and update ``pressed_keys``.

        Merge press and release events by timestamp before processing —
        without this, a tap whose press+release land in the same
        dispatch_events() cycle would have its release processed before
        the press (the buffers used to be drained sequentially), so the
        release would be dropped and the key would stay "pressed" forever.
        """
        exp_win.winHandle.dispatch_events()

        merged = (
            [(t, k, "press") for k, t in _keyPressBuffer]
            + [(t, k, "release") for k, t in _keyReleaseBuffer]
        )
        merged.sort()
        _keyPressBuffer.clear()
        _keyReleaseBuffer.clear()

        # core.monotonicClock is shared across PsychoPy; task_timer is local
        # to the current task. We log keypress onsets in the task's
        # reference frame, hence the offset.
        clock_offset = core.monotonicClock._timeAtLastReset - task_timer._timeAtLastReset

        self._new_key_pressed = []
        for t, k, kind in merged:
            if kind == "press":
                self.pressed_keys[k] = (k, t)
                self._new_key_pressed.append((k, t))
            else:
                if k in self.pressed_keys:
                    press_t = self.pressed_keys[k][1]
                    self._events.append({
                        "trial_type": "keypress",
                        "key": k,
                        "onset": press_t + clock_offset,
                        "offset": t + clock_offset,
                        "duration": t - press_t,
                        "sample": time.monotonic(),
                    })
                    del self.pressed_keys[k]

    def held_for(self, key_set: list[str]) -> list[bool]:
        """Return a list of booleans, one per key_set entry, indicating
        which buttons are currently held down. Pass straight to ``emulator.step()``."""
        return [k in self.pressed_keys for k in key_set]

    def new_keys_pressed(self) -> list[str]:
        """Keys pressed *since the last poll*. Useful for menu navigation
        (questionnaire, prompts) that want edge-triggered behaviour."""
        return [k for k, _ in self._new_key_pressed]

    def drain_events(self) -> list[dict]:
        """Return accumulated keypress events and reset the buffer."""
        out = self._events
        self._events = []
        return out
