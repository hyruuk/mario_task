"""EEG / iEEG marker dispatch with three transport backends.

Three transports, selected at runtime via :func:`configure`:

* ``"lsl"`` — pylsl :class:`StreamOutlet`, int32 channel. Default.
* ``"serial"`` — pyserial write to a port (e.g. ``/dev/ttyACM0``, ``COM3``). One byte per marker.
* ``"parallel"`` — pyparallel write to ``/dev/parport1``. Linux only.
* ``"null"`` — drop all markers (dev / offline). Never raises.

Marker codes (single positive byte; same value on every transport):

==========  ==========================================================
  0         TASK_START — once when ``task.run()`` enters its loop
  1         TASK_STOP  — once when ``task.run()`` exits
  2         GAME_RESET — once per ``emulator.reset()``
  3         NON_GAME_FLIP — heartbeat on every non-gameplay PsychoPy flip
  4..15     reserved
 16..255    GAME_FRAME — gameplay heartbeat, ``16 + (level_step % 240)``,
              pushed once per ``emulator.step()`` so marker count exactly
              matches BK2 frame count even when render frames are dropped.
              The byte wraps every 240 frames (~4 s @ 60 Hz NES); the
              bk2 disambiguates the absolute frame index.
==========  ==========================================================

Lifecycle codes (0–3) and gameplay codes (16–255) are disjoint by design
so analysts can split the stream without ambiguity.

If a backend fails to initialize (LSL daemon unreachable, serial port
missing, etc.), :func:`configure` automatically substitutes
:class:`_NullBackend`, logs a warning, and execution continues. This
prevents an in-progress session from crashing because a marker stream
went away; the operator sees the warning at the start of the session.

The module is pure-Python and has at most ``pylsl`` / ``pyserial`` /
``pyparallel`` as optional runtime deps. Test code can pre-set
``_backend`` directly to inject a stub.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Marker codes
# ---------------------------------------------------------------------------

TASK_START = 0
TASK_STOP = 1
GAME_RESET = 2
NON_GAME_FLIP = 3

_GAME_FRAME_BASE = 16
_GAME_FRAME_MOD = 240  # 256 - _GAME_FRAME_BASE


def encode_frame(level_step: int) -> int:
    """Encode a gameplay frame index as a single positive byte in [16, 255]."""
    return _GAME_FRAME_BASE + (int(level_step) % _GAME_FRAME_MOD)


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
    """One byte per marker over a parallel port. Linux-only (no pyparallel
    Windows support). Timestamps ignored — amplifier stamps on byte arrival."""

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
) -> _Backend:
    """Pick the active marker transport. Returns the resolved backend.

    Args:
        backend: ``"lsl"``, ``"serial"``, ``"parallel"``, or ``"null"``.
        port:    Device path. Required for serial / parallel.
        stream:  LSL stream identity. Optional; defaults to ``StreamConfig()``.

    On init failure (LSL daemon unreachable, serial port missing,
    pyparallel not installed on Windows, etc.) the function falls back to
    :class:`_NullBackend`, logs a warning, and returns. Caller code can
    keep running.
    """
    global _backend
    backend = backend.lower()
    try:
        if backend == "lsl":
            _backend = _LSLBackend(stream or StreamConfig())
        elif backend == "serial":
            if not port:
                raise ValueError("`port` is required for the serial backend.")
            _backend = _SerialBackend(port)
        elif backend == "parallel":
            if sys.platform.startswith("win"):
                raise RuntimeError(
                    "parallel-port markers are not supported on Windows."
                )
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
    """Drop the active backend. Used by tests; not part of the public API."""
    global _backend
    _backend = None
