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
    decode_marker,
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


def test_encode_frame_wraps_with_default_mod_8() -> None:
    assert encode_frame(0) == 16
    assert encode_frame(1) == 17
    assert encode_frame(7) == 23
    assert encode_frame(8) == 16  # wraps
    assert encode_frame(9) == 17
    assert encode_frame(17) == 17  # wraps twice


def test_lifecycle_and_gameplay_codes_are_disjoint() -> None:
    lifecycle = {TASK_START, TASK_STOP, GAME_RESET, NON_GAME_FLIP}
    # Defaults: base=16, mod=8 → gameplay codes 16..23.
    gameplay = {encode_frame(i) for i in range(0, 100)}
    assert lifecycle & gameplay == set()
    # Lifecycle is below the gameplay floor.
    assert all(v < 16 for v in lifecycle)
    assert all(16 <= v < 24 for v in gameplay)


def test_decode_marker_lifecycle_labels() -> None:
    assert decode_marker(TASK_START) == "TASK_START"
    assert decode_marker(TASK_STOP) == "TASK_STOP"
    assert decode_marker(GAME_RESET) == "GAME_RESET"
    assert decode_marker(NON_GAME_FLIP) == "NON_GAME_FLIP"


def test_decode_marker_game_frame_includes_phase() -> None:
    # Default codes: base=16, mod=8.
    assert "%8=0" in decode_marker(encode_frame(0))
    assert "%8=7" in decode_marker(encode_frame(7))
    # Wrap point.
    assert "%8=0" in decode_marker(encode_frame(8))


def test_decode_marker_unknown_value() -> None:
    # Values in the reserved 4..15 band are flagged as UNKNOWN (with default codes).
    assert decode_marker(5).startswith("UNKNOWN")
    assert decode_marker(15).startswith("UNKNOWN")


# ---------------------------------------------------------------------------
# Configurable trigger codes
# ---------------------------------------------------------------------------


def test_custom_codes_affect_encode_and_decode() -> None:
    """set_codes overrides the active scheme; encode/decode follow."""
    custom = markers.TriggerCodes(
        task_start=10, task_stop=11, game_reset=12, non_game_flip=13,
        game_frame_base=64, game_frame_mod=16,
    )
    markers.set_codes(custom)
    try:
        assert markers.TASK_START == 10
        assert markers.NON_GAME_FLIP == 13
        assert markers.GAME_FRAME_BASE == 64
        assert markers.GAME_FRAME_MOD == 16
        assert encode_frame(0) == 64
        assert encode_frame(15) == 79
        assert encode_frame(16) == 64  # wraps at the new mod
        assert decode_marker(10) == "TASK_START"
        assert "%16=3" in decode_marker(encode_frame(3))
    finally:
        # Reset for other tests.
        markers._reset_for_tests()


def test_configure_with_codes_applies_them() -> None:
    custom = markers.TriggerCodes(game_frame_mod=4)
    markers.configure("null", codes=custom)
    try:
        # mod=4 → codes cycle 16..19.
        assert encode_frame(0) == 16
        assert encode_frame(4) == 16
        assert markers.GAME_FRAME_MOD == 4
    finally:
        markers._reset_for_tests()


def test_module_getattr_raises_for_unknown_name() -> None:
    with pytest.raises(AttributeError):
        markers.NOT_A_REAL_CONSTANT  # type: ignore[attr-defined]


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


# ---------------------------------------------------------------------------
# trigger_every (gameplay marker decimation)
# ---------------------------------------------------------------------------


def test_trigger_every_defaults_to_one() -> None:
    assert markers.get_trigger_every() == 1


def test_configure_sets_trigger_every() -> None:
    markers.configure("null", trigger_every=4)
    assert markers.get_trigger_every() == 4


def test_configure_without_trigger_every_keeps_previous() -> None:
    markers.set_trigger_every(3)
    markers.configure("null")  # no trigger_every kwarg
    assert markers.get_trigger_every() == 3


def test_set_trigger_every_rejects_zero_and_negative() -> None:
    with pytest.raises(ValueError):
        markers.set_trigger_every(0)
    with pytest.raises(ValueError):
        markers.set_trigger_every(-1)


def test_trigger_every_decimation_pattern() -> None:
    """For trigger_every=4, frames 1, 5, 9, 13 emit; byte cycles per send."""
    markers.set_trigger_every(4)
    n = markers.get_trigger_every()
    sent = []
    for level_step in range(1, 14):
        if (level_step - 1) % n == 0:
            trigger_idx = (level_step - 1) // n
            sent.append((level_step, encode_frame(trigger_idx)))
    assert [s[0] for s in sent] == [1, 5, 9, 13]
    # Byte values cycle per *sent* trigger, not per emulator frame.
    assert [s[1] for s in sent] == [16, 17, 18, 19]


def test_send_signal_with_no_prior_configure_uses_lazy_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling send_signal() before configure() must auto-init (LSL default).

    On a box without an actual LSL daemon the init may succeed (pylsl is
    self-contained) or fall through to NullBackend; either way we must not
    raise from send_signal.
    """
    markers._reset_for_tests()
    markers.send_signal(TASK_START)
    assert markers._backend is not None
