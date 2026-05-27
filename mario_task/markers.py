"""EEG / iEEG marker dispatch with three transport backends.

Three transports, selected at runtime via :func:`configure`:

* ``"lsl"`` — pylsl :class:`StreamOutlet`, int32 channel. Default.
* ``"serial"`` — pyserial write to a port (e.g. ``/dev/ttyACM0``, ``COM3``). One byte per marker.
* ``"parallel"`` — pyparallel write to ``/dev/parport1``. Linux only.
* ``"null"`` — drop all markers (dev / offline). Never raises.

Marker code scheme (single positive byte; same value on every transport):

* Lifecycle codes (default 0–3): ``task_start``, ``task_stop``, ``game_reset``,
  ``non_game_flip``.
* Gameplay codes (default 16 + (level_step % 8)): pushed once per
  ``emulator.step()`` so marker count exactly matches BK2 frame count
  even when render frames are dropped. The byte wraps every
  ``game_frame_mod`` frames; the bk2 disambiguates the absolute index.
* Lifecycle and gameplay codes are disjoint by construction (lifecycle
  values < ``game_frame_base``, gameplay values ≥ ``game_frame_base``).

All six numeric codes plus the modulo are user-editable via the
:class:`TriggerCodes` dataclass — populated from ``config.json`` and
passed to :func:`configure`. Module-level constants like
``markers.TASK_START`` resolve dynamically via :func:`__getattr__`, so
code reading ``markers.TASK_START`` always sees the *currently
configured* value. Avoid ``from mario_task.markers import TASK_START``
imports — those capture the value at import time and won't update if
:func:`configure` is called later.

If a backend fails to initialize (LSL daemon unreachable, serial port
missing, etc.), :func:`configure` automatically substitutes
:class:`_NullBackend`, logs a warning, and execution continues. This
prevents an in-progress session from crashing because a marker stream
went away; the operator sees the warning at the start of the session.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trigger code scheme (configurable via config.json -> Settings.triggers.codes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriggerCodes:
    """Single-byte marker code scheme.

    Constraints (enforced by validation in :mod:`mario_task.settings`):
        * Every code must fit in ``[0, 255]``.
        * Lifecycle codes (``task_start``, ``task_stop``, ``game_reset``,
          ``non_game_flip``) must all be strictly below ``game_frame_base``
          so gameplay markers can never collide with lifecycle markers.
        * ``game_frame_base + game_frame_mod`` must fit in ``[0, 256]``
          (i.e. gameplay codes stay within a single byte).
        * Lifecycle codes must be distinct.

    The ``game_frame_mod`` default of 8 produces 8 distinct rapidly-cycling
    gameplay codes (16..23 by default), wrapping every ~133 ms at 60 Hz.
    The wrap is harmless because the bk2 stores absolute frame indices;
    the marker stream just lets analysts find frame boundaries cheaply.
    """

    task_start: int = 0
    task_stop: int = 1
    game_reset: int = 2
    non_game_flip: int = 3
    game_frame_base: int = 16
    game_frame_mod: int = 8


# Active code scheme. Mutated by :func:`configure` (with the codes loaded
# from config.json). The module's __getattr__ exposes the individual codes
# under the canonical UPPERCASE names so legacy ``markers.TASK_START``
# accesses keep working.
_codes: TriggerCodes = TriggerCodes()

# Decimation factor for gameplay markers. 1 = every emulator frame; N = one
# trigger per N frames. The engine reads this via :func:`get_trigger_every`
# and decides whether to call :func:`send_signal` on each step.
_trigger_every: int = 1


def set_codes(codes: TriggerCodes) -> None:
    """Override the active trigger code scheme.

    Normally :func:`configure` handles this; :func:`set_codes` is exposed
    for tests and for callers (e.g. monitor) that need to read different
    codes than the experiment is publishing.
    """
    global _codes
    _codes = codes


def get_codes() -> TriggerCodes:
    """Return the currently active :class:`TriggerCodes`."""
    return _codes


def set_trigger_every(n: int) -> None:
    """Override the gameplay-marker decimation factor.

    Normally :func:`configure` handles this. Exposed for tests.
    """
    global _trigger_every
    if n < 1:
        raise ValueError(f"trigger_every must be ≥ 1, got {n}")
    _trigger_every = int(n)


def get_trigger_every() -> int:
    """Return the active gameplay-marker decimation factor (1 = every frame)."""
    return _trigger_every


def __getattr__(name: str) -> Any:
    """Expose the trigger codes as module-level constants.

    Lets call sites use ``markers.TASK_START`` (preferred) instead of
    ``markers.get_codes().task_start``. Module-level ``__getattr__``
    re-resolves on every access, so the values reflect the most recent
    :func:`configure` / :func:`set_codes` call.
    """
    if name == "TASK_START":
        return _codes.task_start
    if name == "TASK_STOP":
        return _codes.task_stop
    if name == "GAME_RESET":
        return _codes.game_reset
    if name == "NON_GAME_FLIP":
        return _codes.non_game_flip
    if name == "GAME_FRAME_BASE":
        return _codes.game_frame_base
    if name == "GAME_FRAME_MOD":
        return _codes.game_frame_mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def encode_frame(level_step: int) -> int:
    """Encode a gameplay frame index as a single positive byte.

    Returns ``codes.game_frame_base + (level_step % codes.game_frame_mod)``,
    using the currently active :class:`TriggerCodes`.
    """
    return _codes.game_frame_base + (int(level_step) % _codes.game_frame_mod)


def decode_marker(value: int) -> str:
    """Return a human-readable label for a marker byte.

    Used by :mod:`mario_task.monitor` to print readable output, and by
    anyone post-processing a LabRecorder XDF file who wants a string
    column alongside the integer.

    Uses the currently active :class:`TriggerCodes`. If you're decoding
    a recording made with a different code scheme, call :func:`set_codes`
    with the matching scheme first.
    """
    if value == _codes.task_start:
        return "TASK_START"
    if value == _codes.task_stop:
        return "TASK_STOP"
    if value == _codes.game_reset:
        return "GAME_RESET"
    if value == _codes.non_game_flip:
        return "NON_GAME_FLIP"
    gf_end = _codes.game_frame_base + _codes.game_frame_mod
    if _codes.game_frame_base <= value < gf_end:
        return f"GAME_FRAME[%{_codes.game_frame_mod}={value - _codes.game_frame_base}]"
    return f"UNKNOWN({value})"


EEG_MARKERS_ON_FLIP = True
"""Whether the task base class should push a heartbeat marker on every flip.

Kept True at module level for parity with upstream; flip-back to False in
tests if a particular task should suppress per-flip markers (gameplay
overrides this anyway by returning ``None`` from ``_eeg_marker_value``)."""


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------


class _Backend(Protocol):
    """All backends implement a single :meth:`send` method."""

    def send(self, value: int, timestamp: float | None = None) -> None: ...


@dataclass(frozen=True)
class StreamConfig:
    """LSL-specific stream identity (ignored by serial / parallel)."""

    name: str = "mario_task"
    type: str = "Markers"
    source_id: str = "mario_task_markers"


# ---------------------------------------------------------------------------
# Concrete backends
# ---------------------------------------------------------------------------


class _LSLBackend:
    """LSL outlet on an int32 channel. Honors per-event timestamps."""

    def __init__(self, stream: StreamConfig) -> None:
        import pylsl  # imported lazily so non-LSL backends don't need pylsl

        info = pylsl.StreamInfo(
            name=stream.name,
            type=stream.type,
            channel_count=1,
            nominal_srate=pylsl.IRREGULAR_RATE,
            channel_format=pylsl.cf_int32,
            source_id=stream.source_id,
        )
        chans = info.desc().append_child("channels")
        ch = chans.append_child("channel")
        ch.append_child_value("label", "marker")
        ch.append_child_value("unit", "code")
        ch.append_child_value("type", "Marker")
        self._outlet = pylsl.StreamOutlet(info)

    def send(self, value: int, timestamp: float | None = None) -> None:
        # pylsl interprets timestamp=0.0 as "stamp at push time"; an explicit
        # local_clock() value pins the sample to the event time.
        ts = 0.0 if timestamp is None else float(timestamp)
        self._outlet.push_sample([int(value)], ts)


class _SerialBackend:
    """One byte per marker over a serial port. Timestamps ignored — the
    EEG amplifier stamps on byte arrival."""

    def __init__(self, port_address: str) -> None:
        import serial

        self._port_address = port_address
        self._port = serial.Serial(port_address)

    def send(self, value: int, timestamp: float | None = None) -> None:
        self._port.write((int(value) & 0xFF).to_bytes(1, byteorder="big"))


class _ParallelBackend:
    """One byte per marker over a parallel port. Timestamps ignored —
    amplifier stamps on byte arrival."""

    def __init__(self, port_address: str) -> None:
        import parallel  # pyparallel

        self._port_address = port_address
        # Some forks accept positional, some require keyword.
        try:
            self._port = parallel.Parallel(port_address)
        except TypeError:
            self._port = parallel.Parallel(port=port_address)

    def send(self, value: int, timestamp: float | None = None) -> None:
        self._port.setData(int(value) & 0xFF)


class _NullBackend:
    """No-op backend used when a real one is unavailable.

    Logs the very first marker push (to make it obvious in the log that
    markers are being dropped) but stays silent thereafter to avoid log
    spam during a 10-minute run at 60 markers/sec.
    """

    def __init__(self, reason: str = "no backend configured") -> None:
        self._reason = reason
        self._warned = False

    def send(self, value: int, timestamp: float | None = None) -> None:
        if not self._warned:
            logger.warning(
                "EEG markers are being dropped (%s). First dropped value=%d.",
                self._reason,
                value,
            )
            self._warned = True


# ---------------------------------------------------------------------------
# Module-level state & configuration
# ---------------------------------------------------------------------------

_backend: _Backend | None = None


def configure(
    backend: str = "lsl",
    port: str | None = None,
    stream: StreamConfig | None = None,
    codes: TriggerCodes | None = None,
    trigger_every: int | None = None,
) -> _Backend:
    """Pick the active marker transport. Returns the resolved backend.

    Args:
        backend: ``"lsl"``, ``"serial"``, ``"parallel"``, or ``"null"``.
        port:    Device path. Required for serial / parallel.
        stream:  LSL stream identity. Optional; defaults to ``StreamConfig()``.
        codes:   :class:`TriggerCodes` defining the marker code scheme.
                 When set, the new codes apply globally to subsequent
                 :func:`send_signal`, :func:`encode_frame`, and
                 :func:`decode_marker` calls. ``None`` leaves the active
                 codes unchanged (defaults to :class:`TriggerCodes()` at
                 module import time).

    On init failure (LSL daemon unreachable, serial port missing,
    pyparallel not installed, etc.) the function falls back to
    :class:`_NullBackend`, logs a warning, and returns. Caller code can
    keep running.
    """
    global _backend
    if codes is not None:
        set_codes(codes)
    if trigger_every is not None:
        set_trigger_every(trigger_every)
    backend = backend.lower()
    try:
        if backend == "lsl":
            _backend = _LSLBackend(stream or StreamConfig())
        elif backend == "serial":
            if not port:
                raise ValueError("`port` is required for the serial backend.")
            _backend = _SerialBackend(port)
        elif backend == "parallel":
            if not port:
                raise ValueError("`port` is required for the parallel backend.")
            _backend = _ParallelBackend(port)
        elif backend == "null":
            _backend = _NullBackend(reason="backend=null (explicit)")
        else:
            raise ValueError(f"unknown backend: {backend!r}")
    except Exception as exc:
        logger.warning(
            "Falling back to NullBackend (markers will be dropped): %s", exc
        )
        _backend = _NullBackend(reason=f"{backend} init failed: {exc}")
    return _backend


def get_outlet() -> Any:
    """Return the underlying ``pylsl.StreamOutlet`` (or ``None`` for non-LSL backends).

    Compatibility shim for callers that want to force LSL init early so
    downstream tooling (LabRecorder) has time to discover the outlet.
    """
    if _backend is None:
        configure("lsl")
    return getattr(_backend, "_outlet", None)


def send_signal(data: int, timestamp: float | None = None) -> None:
    """Push integer ``data`` to the active backend.

    ``timestamp`` is forwarded to LSL so the sample carries the true
    event time (use :func:`now` to capture it). Serial / parallel ignore
    it (the amplifier stamps on byte arrival).
    """
    if _backend is None:
        configure("lsl")
    assert _backend is not None
    _backend.send(data, timestamp=timestamp)


def now() -> float:
    """LSL-clock timestamp at the moment of the event.

    Call this *immediately* after the event of interest (e.g. right after
    ``emulator.step()``), then pass the result to :func:`send_signal`.
    """
    import pylsl  # lazy import: tests that don't touch markers don't need LSL
    return pylsl.local_clock()


def _reset_for_tests() -> None:
    """Drop the active backend AND reset codes to defaults. Used by tests."""
    global _backend, _codes, _trigger_every
    _backend = None
    _codes = TriggerCodes()
    _trigger_every = 1
