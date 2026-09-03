"""Covers the operator shortcuts: Ctrl+C / Ctrl+N / Ctrl+Q.

These are the only way out of a run once a subject is mid-level, so the
two halves are pinned down separately:

* :func:`session._listen_shortcuts` decides *what a key means*, and must not
  eat the sync signal the scanner waiter is watching for;
* :func:`input._on_pyglet_key_press` decides *which keys still reach PsychoPy*
  while the participant's keystrokes are being captured for the task.

Neither needs a window.
"""

from __future__ import annotations

import pytest
from conftest import import_or_skip

# session pulls in psychopy; skip (rather than error) where that cannot import,
# e.g. the headless CI runner.
session = import_or_skip("mario_task.session", reason="the shortcut layer is built on psychopy")

import pyglet  # noqa: E402
from psychopy import event  # noqa: E402

from mario_task import input as I  # noqa: E402


def _stub_keys(monkeypatch, pressed):
    """Make ``event.getKeys`` report ``pressed``; record the key list it got."""
    seen = {}

    def getKeys(keyList=None, modifiers=False, timeStamped=False):  # noqa: N803
        seen["keyList"] = keyList
        return [(k, m) for k, m in pressed if keyList is None or k in keyList]

    monkeypatch.setattr(event, "getKeys", getKeys)
    return seen


# ---------------------------------------------------------------------------
# What a key means
# ---------------------------------------------------------------------------


def test_nothing_pressed_is_not_a_shortcut(monkeypatch):
    _stub_keys(monkeypatch, [])
    assert session._listen_shortcuts() is None


def test_escape_does_nothing(monkeypatch):
    """Ctrl+Q is the only way out; a bare Escape must not end the session."""
    _stub_keys(monkeypatch, [("escape", {})])
    assert session._listen_shortcuts() is None


def test_ctrl_q_quits(monkeypatch):
    _stub_keys(monkeypatch, [("q", {"ctrl": True})])
    assert session._listen_shortcuts() == "q"


@pytest.mark.parametrize("name", ["c", "n", "q"])
def test_a_bare_letter_is_not_a_shortcut(monkeypatch, name):
    """A stray 'q' from a participant must not end the session."""
    _stub_keys(monkeypatch, [(name, {"ctrl": False})])
    assert session._listen_shortcuts() is None


@pytest.mark.parametrize(("name", "expected"), [("c", "c"), ("n", "n")])
def test_ctrl_c_and_ctrl_n(monkeypatch, name, expected):
    _stub_keys(monkeypatch, [(name, {"ctrl": True})])
    assert session._listen_shortcuts() == expected


# ---------------------------------------------------------------------------
# The Ctrl+Q global key
# ---------------------------------------------------------------------------


@pytest.fixture
def quit_key():
    """Register the global key the way a session does, and clean it up.

    ``event.clearEvents()`` matters: these cases dispatch real keypresses
    through PsychoPy's own handler, which leaves them in its key buffer. A
    leftover ctrl-modified 'q' would read as a quit in whatever test ran next.
    """
    event.clearEvents()
    session._install_quit_key()
    yield
    session._remove_quit_key()
    session._quit_requested = False
    event.clearEvents()


def test_the_quit_key_is_registered_and_removed(quit_key):
    assert ("q", ("ctrl",)) in [(k.key, k.modifiers) for k in event.globalKeys]
    session._remove_quit_key()
    assert list(event.globalKeys) == []


def test_removing_the_quit_key_twice_is_harmless(quit_key):
    session._remove_quit_key()
    session._remove_quit_key()


def test_ctrl_q_is_caught_between_polls(monkeypatch, quit_key):
    """The point of the global key: PsychoPy runs it the moment it arrives.

    Dispatched through PsychoPy's own handler, exactly as pyglet delivers it.
    """
    _stub_keys(monkeypatch, [])  # nothing left in the buffer to find
    event._onPygletKey(pyglet.window.key.Q, pyglet.window.key.MOD_CTRL)
    assert session._quit_requested is True
    assert session._listen_shortcuts() == "q"


def test_the_quit_request_is_consumed_once(monkeypatch, quit_key):
    """A remembered Ctrl+Q must not re-fire on the next frame."""
    _stub_keys(monkeypatch, [])
    session._request_quit()
    assert session._listen_shortcuts() == "q"
    assert session._listen_shortcuts() is None


def test_the_quit_key_does_not_kill_the_process(quit_key):
    """It raises a flag; the clean teardown is what actually ends the session."""
    session._request_quit()  # would be core.quit() in a naive implementation
    assert session._quit_requested is True


def test_polling_only_claims_the_shortcut_keys(monkeypatch):
    """PsychoPy leaves unlisted keys in the buffer — so the list must be narrow.

    Polling every frame with ``getKeys()`` (no list) would clear the buffer and
    swallow the scanner trigger that ``sync``'s keyboard waiter is polling for
    on the very same frames.
    """
    seen = _stub_keys(monkeypatch, [])
    session._listen_shortcuts()
    assert seen["keyList"] == ["n", "c", "q"]


# ---------------------------------------------------------------------------
# Which keys still reach PsychoPy mid-run
# ---------------------------------------------------------------------------


@pytest.fixture
def forwarded(monkeypatch):
    """Record what the pyglet hook hands on to PsychoPy's own handler."""
    captured: list[tuple[int, int]] = []
    monkeypatch.setattr(
        event, "_onPygletKey", lambda symbol, modifier, *a, **kw: captured.append((symbol, modifier))
    )
    _clear()
    yield captured
    _clear()


def _clear() -> None:
    """Empty the module-level pyglet buffers between cases."""
    I._keyPressBuffer.clear()
    I._keyReleaseBuffer.clear()


def test_escape_is_not_forwarded_to_psychopy(forwarded):
    """An unmodified Escape is just another keystroke; it must not quit."""
    I._on_pyglet_key_press(pyglet.window.key.ESCAPE, 0)
    assert forwarded == []
    assert I._keyPressBuffer[0][0] == "escape"


def test_a_gamepad_button_is_not_forwarded(forwarded):
    """The subject's keystrokes belong to the emulator, not PsychoPy's buffer."""
    I._on_pyglet_key_press(pyglet.window.key.X, 0)   # X = jump
    assert forwarded == []
    assert I._keyPressBuffer[0][0] == "x"


def test_a_modified_key_is_forwarded(forwarded):
    I._on_pyglet_key_press(pyglet.window.key.Q, pyglet.window.key.MOD_CTRL)
    assert forwarded == [(pyglet.window.key.Q, pyglet.window.key.MOD_CTRL)]


# ---------------------------------------------------------------------------
# Regression: the modifier belongs to the key, not to the batch
# ---------------------------------------------------------------------------


def test_a_stray_q_does_not_hijack_ctrl_c(monkeypatch):
    """An unmodified 'q' sitting in the buffer must not turn Ctrl+C into a quit.

    The old implementation asked "was ctrl held for *any* of these keys" and
    then returned the *first* key's name, so an abort became a session quit.
    Worse, it only drained the buffer once a ctrl key was already in it, so a
    stray 'q' waited there indefinitely for a shortcut to hijack.
    """
    _stub_keys(monkeypatch, [("q", {"ctrl": False}), ("c", {"ctrl": True})])
    assert session._listen_shortcuts() == "c"


def test_a_stray_q_does_not_hijack_ctrl_n(monkeypatch):
    _stub_keys(monkeypatch, [("q", {"ctrl": False}), ("n", {"ctrl": True})])
    assert session._listen_shortcuts() == "n"


def test_bare_letters_are_drained_rather_than_left_to_accumulate(monkeypatch):
    """Polling must consume the keys it was asked about, every frame."""
    seen = _stub_keys(monkeypatch, [("q", {"ctrl": False})])
    assert session._listen_shortcuts() is None
    # getKeys was actually called (and so drained), not gated behind a peek
    # at psychopy's private _keyBuffer.
    assert seen["keyList"] == ["n", "c", "q"]
