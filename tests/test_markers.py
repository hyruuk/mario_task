"""Tests for the EEG marker dispatch module.

Pure-Python; no display, no psychopy, no actual LSL outlet. Each test
that touches the configure path also calls ``_reset_for_tests`` so
state doesn't leak between cases.
"""

from __future__ import annotations

import logging

import pytest

from mario_task import markers
from mario_task.markers import (
    GAME_RESET,
    NON_GAME_FLIP,
    TASK_START,
    TASK_STOP,
    _NullBackend,
    encode_frame,
)


@pytest.fixture(autouse=True)
def _reset_backend() -> None:
    markers._reset_for_tests()


# ---------------------------------------------------------------------------
# Marker code disjointness
# ---------------------------------------------------------------------------


def test_lifecycle_codes_have_expected_values() -> None:
    assert TASK_START == 0
    assert TASK_STOP == 1
    assert GAME_RESET == 2
    assert NON_GAME_FLIP == 3


def test_encode_frame_wraps_at_240() -> None:
    assert encode_frame(0) == 16
    assert encode_frame(1) == 17
    assert encode_frame(239) == 255
    assert encode_frame(240) == 16  # wraps
    assert encode_frame(241) == 17
    assert encode_frame(481) == 17  # wraps twice


def test_lifecycle_and_gameplay_codes_are_disjoint() -> None:
    lifecycle = {TASK_START, TASK_STOP, GAME_RESET, NON_GAME_FLIP}
    gameplay = {encode_frame(i) for i in range(0, 240)}
    assert lifecycle & gameplay == set()
    # Gameplay always fits in a positive byte.
    assert all(16 <= v <= 255 for v in gameplay)
    # Lifecycle is below the gameplay floor.
    assert all(v < 16 for v in lifecycle)


def test_encode_frame_accepts_numpy_like_ints() -> None:
    # The emulator step counter may be a numpy int; encode_frame() coerces.
    class FakeNpInt:
        def __init__(self, v: int) -> None:
            self._v = v

        def __int__(self) -> int:
            return self._v

    assert encode_frame(FakeNpInt(7)) == 23  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Null backend
# ---------------------------------------------------------------------------


def test_null_backend_does_not_raise_on_send() -> None:
    nb = _NullBackend()
    nb.send(0)
    nb.send(255, timestamp=12345.0)


def test_null_backend_warns_only_once(caplog: pytest.LogCaptureFixture) -> None:
    nb = _NullBackend(reason="testing")
    with caplog.at_level(logging.WARNING, logger="mario_task.markers"):
        for v in range(5):
            nb.send(v)
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1
    assert "testing" in warns[0].getMessage()


def test_configure_null_backend_explicitly() -> None:
    markers.configure("null")
    # send_signal must not raise.
    markers.send_signal(TASK_START)
    markers.send_signal(encode_frame(5))


# ---------------------------------------------------------------------------
# Configure fallback behavior
# ---------------------------------------------------------------------------


def test_configure_serial_without_port_raises_and_falls_back(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="mario_task.markers"):
        backend = markers.configure("serial", port=None)
    assert isinstance(backend, _NullBackend)
    # The fallback warning was emitted at configure time.
    assert any("Falling back" in r.getMessage() for r in caplog.records)


def test_configure_unknown_backend_falls_back(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="mario_task.markers"):
        backend = markers.configure("bluetooth")  # not a thing
    assert isinstance(backend, _NullBackend)


def test_configure_serial_with_missing_port_device_falls_back(caplog: pytest.LogCaptureFixture) -> None:
    # /dev/this-port-does-not-exist will make pyserial raise; we expect a
    # warning and a NullBackend, not a crash.
    with caplog.at_level(logging.WARNING, logger="mario_task.markers"):
        backend = markers.configure("serial", port="/dev/this-port-does-not-exist")
    assert isinstance(backend, _NullBackend)


def test_send_signal_with_no_prior_configure_uses_lazy_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling send_signal() before configure() must auto-init (LSL default).

    On a box without an actual LSL daemon the init may succeed (pylsl is
    self-contained) or fall through to NullBackend; either way we must not
    raise from send_signal.
    """
    markers._reset_for_tests()
    markers.send_signal(TASK_START)
    assert markers._backend is not None
