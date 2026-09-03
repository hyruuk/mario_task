"""Aligning the run start with the recording device.

``sync.mode`` says what happens at run start:

* ``none`` — start immediately. The default.
* ``wait`` — block until the sync signal arrives, optionally discarding a few
  dummy scans first.
* ``send`` — the stimulus computer starts the recording: a single value goes
  out at run start. This is the current fMRI setup: send ``s`` to a serial
  port, which the scanner reads as "go".

``sync.backend`` says over what, and defaults to ``none`` — no hardware. In
``wait`` mode the signal is then expected from the **keyboard**, which is also
how most MR trigger boxes present themselves; in ``send`` mode there is nothing
to send to, so the run starts immediately with a warning.

``sync.signal`` is the signal itself and means the same thing in both
directions: what we send, or what we listen for.

Sync runs **once per gameplay run**, and exactly one thing gates the start:

* ``wait`` — the scanner. The subject sees only "Waiting for the scanner";
  the usual "press X when ready" screen is skipped entirely, because a run
  the trigger has already released should not need a second go-ahead.
* ``send`` — us, once the subject is ready: the prompt comes first, then the
  start pulse, so the scanner is not left running while they read.
* ``none`` — the subject, via the prompt. The desk case.

Both screens read the keyboard through ``event.getKeys``, which keeps working
until :func:`mario_task.input.install` replaces PsychoPy's key handler inside
``task.run()``.

Why :meth:`Sync.start` is a generator
-------------------------------------
It yields once per frame rather than blocking, so the session's normal frame
loop keeps running while we wait. That means the window stays responsive and
the operator's Ctrl+Q still aborts — a blocking ``wait_for_ttl`` would sit
unresponsive until the scanner fired, which is awkward when a sequence gets
cancelled.

Like :mod:`mario_task.markers`, hardware problems degrade rather than abort: a
port that is unset or will not open logs a warning and falls back to the
``backend="none"`` behaviour above.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from typing import TYPE_CHECKING, Any

from mario_task import markers

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psychopy.visual import Window

    from mario_task.settings import SyncSettings

logger = logging.getLogger(__name__)


def _as_byte(value: str | int) -> int:
    """Coerce ``sync.signal`` to the single byte that goes on the wire.

    A one-character string becomes its ordinal (``"s"`` -> 115); a numeric
    string or int is used directly. Longer strings are rejected loudly,
    because silently sending only the first character would be a nasty
    surprise at scan time.

    >>> _as_byte("s")
    115
    >>> _as_byte("255")
    255
    """
    if isinstance(value, int):
        return value & 0xFF
    text = str(value)
    if text.isdigit():
        return int(text) & 0xFF
    if len(text) == 1:
        return ord(text) & 0xFF
    raise ValueError(
        f"sync.signal must be a single character (e.g. 's') or a number "
        f"0-255 to be sent, got {value!r}."
    )


# ---------------------------------------------------------------------------
# Senders
# ---------------------------------------------------------------------------


class _MarkerSender:
    """Re-use the already-open outgoing marker backend.

    Lets one serial device carry both the scanner start signal and the event
    markers without opening the port twice (which would fail on Linux).
    """

    def __init__(self) -> None:
        self._backend = markers.get_backend()
        if self._backend is None:
            logger.warning(
                "sync.backend='markers' but no marker backend is configured; "
                "the start signal will be dropped."
            )

    def send(self, value: int) -> None:
        if self._backend is not None:
            self._backend.send(value, timestamp=markers.now())

    def close(self) -> None:
        # Not ours to close — markers.close() owns it.
        pass


class _SerialSender:
    def __init__(self, port: str) -> None:
        import serial

        self._port = serial.Serial(port)

    def send(self, value: int) -> None:
        self._port.write(value.to_bytes(1, byteorder="big"))

    def close(self) -> None:
        try:
            self._port.close()
        except Exception:  # noqa: BLE001
            pass


class _ParallelSender:
    def __init__(self, port: str) -> None:
        import parallel

        try:
            self._port = parallel.Parallel(port)
        except TypeError:
            self._port = parallel.Parallel(port=port)

    def send(self, value: int) -> None:
        self._port.setData(value)

    def close(self) -> None:
        pass


class _LSLSender:
    def __init__(self, stream: markers.StreamConfig) -> None:
        self._backend = markers._LSLBackend(stream)

    def send(self, value: int) -> None:
        self._backend.send(value, timestamp=markers.now())

    def close(self) -> None:
        self._backend.close()


class _KeySender:
    """Synthesise a keystroke on the host, for rigs that listen for one.

    Needs ``pynput``, which is not a hard dependency — the failure is caught by
    :func:`configure` and reported as a warning.
    """

    def __init__(self, key: str) -> None:
        from pynput.keyboard import Controller

        self._keyboard = Controller()
        self._key = key

    def send(self, value: int) -> None:
        self._keyboard.press(self._key)
        self._keyboard.release(self._key)

    def close(self) -> None:
        pass


class _NullSender:
    """Stands in for a transport that is absent or broken. Sends nothing.

    ``delivers = False`` so :meth:`Sync._do_send` logs the drop rather than
    reporting a start signal that never left the machine.
    """

    delivers = False

    def __init__(self, reason: str = "") -> None:
        self._reason = reason

    def send(self, value: int) -> None:
        logger.warning("Start signal dropped (%s); run starts unsynchronised.", self._reason)

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Waiters
# ---------------------------------------------------------------------------


class _KeyboardWaiter:
    """Watch the PsychoPy keyboard buffer for a scanner TTL character."""

    def __init__(self, keys: tuple[str, ...]) -> None:
        self._keys = list(keys)

    def prime(self) -> None:
        """Flush stale keypresses so a key held before the run doesn't count."""
        from psychopy import event

        event.clearEvents()

    def poll(self) -> int:
        from psychopy import event

        return len(event.getKeys(self._keys))

    def close(self) -> None:
        pass


class _SerialWaiter:
    def __init__(self, port: str) -> None:
        import serial

        # A short timeout keeps the poll non-blocking so the frame loop and
        # the operator shortcuts stay live.
        self._port = serial.Serial(port, timeout=0)

    def prime(self) -> None:
        self._port.reset_input_buffer()

    def poll(self) -> int:
        waiting = self._port.in_waiting
        return len(self._port.read(waiting)) if waiting else 0

    def close(self) -> None:
        try:
            self._port.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


class Sync:
    """Run-start synchronisation. Build one with :func:`configure`."""

    def __init__(
        self,
        mode: str,
        *,
        sender: Any = None,
        waiter: Any = None,
        value: int = 0,
        n_dummy_scans: int = 0,
        timeout_seconds: float | None = None,
        signal: tuple[str, ...] = (),
        fallback_reason: str = "",
    ) -> None:
        self.mode = mode
        self._sender = sender
        self._waiter = waiter
        self._value = value
        self._n_dummy_scans = n_dummy_scans
        self._timeout = timeout_seconds
        #: The signal being waited on, for logging and tests.
        self.signal = signal
        #: Why we ended up waiting instead of sending; "" when configured so.
        self.fallback_reason = fallback_reason
        #: Wall-clock time the run was cleared to start; ``None`` until then.
        self.sync_time: float | None = None

    @property
    def waits(self) -> bool:
        """True when :meth:`start` may block for an external signal."""
        return self.mode == "wait"

    def start(self, exp_win: Window | None = None) -> Generator[bool, None, float]:
        """Yield once per frame until the run may begin; return the sync time.

        The yielded value is the ``clearBuffer`` flag the session's frame loop
        expects, so this plugs straight into the same driver as the task
        generators.
        """
        if self.mode == "send":
            self._do_send()
        elif self.mode == "wait":
            yield from self._do_wait(exp_win)
        self.sync_time = time.monotonic()
        return self.sync_time

    def _do_send(self) -> None:
        from psychopy import logging as psylog

        try:
            self._sender.send(self._value)
        except Exception as exc:  # noqa: BLE001 - never abort a run over this
            logger.warning("Start signal failed to send: %s", exc)
            psylog.warning(f"sync_send_failed error={exc}")
            return
        if not getattr(self._sender, "delivers", True):
            psylog.warning(f"sync_not_sent value={self._value}")
            return
        logger.info("Start signal sent (value=%d).", self._value)
        psylog.exp(f"sync_sent value={self._value} t={markers.now():.6f}")

    #: What the participant sees while we wait. Deliberately says nothing about
    #: which key we are watching: that is operator information, and it belongs
    #: in the console and the session log (see :func:`_keyboard_waiter`), not on
    #: a screen the participant is looking at.
    WAITING_MESSAGE = "Waiting for the scanner"

    def _waiting_message(self) -> str:
        """Participant-facing text for the waiting screen."""
        return self.WAITING_MESSAGE

    def _do_wait(self, exp_win: Window | None = None) -> Generator[bool, None, None]:
        from psychopy import logging as psylog

        if self._waiter is None:
            return

        # Draw a visible waiting screen, otherwise a fallback looks like a
        # frozen task to whoever is standing at the console.
        message = None
        if exp_win is not None:
            try:
                from psychopy import visual

                message = visual.TextStim(
                    exp_win,
                    text=self._waiting_message(),
                    alignText="center",
                    color="white",
                    units="norm",
                    height=0.06,
                    wrapWidth=2,
                )
            except Exception:  # noqa: BLE001 - the wait matters, the text doesn't
                message = None

        needed = self._n_dummy_scans + 1
        seen = 0
        deadline = None if self._timeout is None else time.monotonic() + self._timeout

        self._waiter.prime()
        logger.info(
            "Waiting for scanner trigger (%d dummy scan%s to discard first)…",
            self._n_dummy_scans,
            "" if self._n_dummy_scans == 1 else "s",
        )
        psylog.exp("waiting_for_scanner")

        while seen < needed:
            count = self._waiter.poll()
            for _ in range(count):
                seen += 1
                psylog.exp(f"scanner_ttl n={seen}")
                if seen >= needed:
                    break
            if seen >= needed:
                break
            if deadline is not None and time.monotonic() > deadline:
                logger.warning(
                    "No scanner trigger after %.1f s; starting unsynchronised.", self._timeout
                )
                psylog.warning("scanner_timeout")
                return
            if message is not None:
                message.draw(exp_win)
                exp_win.flip()
            yield False

        logger.info("Scanner trigger received; starting run.")

    def close(self) -> None:
        """Release any port this object opened. Idempotent."""
        for obj in (self._sender, self._waiter):
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # noqa: BLE001
                    pass
        self._sender = None
        self._waiter = None


def _keyboard_waiter(settings: SyncSettings, reason: str = "") -> Sync:
    """Wait for ``sync.signal`` on the keyboard.

    What ``backend="none"`` means in ``wait`` mode, and where a missing or
    broken serial port lands. Most MR trigger boxes present as a USB keyboard
    anyway, so this is the common scanner case as much as it is the desk one.
    """
    keys = tuple(settings.signal)
    announced = " or ".join(keys) if keys else "a trigger key"
    if reason:
        logger.warning("%s Falling back to the keyboard: press %s to start.", reason, announced)
    else:
        logger.info("Waiting for the sync signal on the keyboard: %s.", announced)
    return Sync(
        "wait",
        waiter=_KeyboardWaiter(keys),
        n_dummy_scans=settings.n_dummy_scans,
        timeout_seconds=settings.timeout_seconds,
        signal=keys,
        fallback_reason=reason,
    )


def _unsent(value: int, reason: str) -> Sync:
    """A ``send`` that has nowhere to go: warn at run start, don't block it.

    Losing the start signal is worth shouting about, but it is not worth
    refusing to run — a session that is merely unsynchronised still yields
    usable gameplay data, whereas one that never started yields none.
    """
    logger.warning("%s The run will start unsynchronised.", reason)
    return Sync("send", sender=_NullSender(reason), value=value)


def configure(settings: SyncSettings, *, stream: markers.StreamConfig | None = None) -> Sync:
    """Build a :class:`Sync` from settings. Never raises.

    A transport that is missing or will not open degrades to the
    ``backend="none"`` behaviour for the mode: ``wait`` falls back to the
    keyboard, ``send`` warns and starts the run anyway.
    """
    mode = settings.mode
    if mode == "none":
        return Sync("none")

    if mode == "send":
        try:
            value = _as_byte(settings.signal[0])
        except ValueError as exc:
            return _unsent(0, str(exc))

        backend = settings.backend
        if backend == "none":
            return _unsent(
                value,
                "sync.mode is 'send' but sync.backend is 'none', so there is "
                "nothing to send the start signal to.",
            )
        if backend in ("serial", "parallel") and not settings.port:
            return _unsent(value, f"sync.backend is {backend!r} but sync.port is not set.")

        try:
            if backend == "markers":
                sender: Any = _MarkerSender()
            elif backend == "serial":
                sender = _SerialSender(settings.port or "")
            elif backend == "parallel":
                sender = _ParallelSender(settings.port or "")
            elif backend == "lsl":
                sender = _LSLSender(stream or markers.StreamConfig())
            elif backend == "key":
                sender = _KeySender(str(settings.signal[0]))
            else:
                raise ValueError(f"unknown sync backend {backend!r}")
        except Exception as exc:  # noqa: BLE001
            return _unsent(value, f"Sync backend {backend!r} failed to open ({exc}).")
        return Sync("send", sender=sender, value=value)

    # mode == "wait". backend "none" and "keyboard" both mean the keyboard.
    if settings.backend != "serial":
        return _keyboard_waiter(settings)

    if not settings.port:
        return _keyboard_waiter(settings, "sync.backend is 'serial' but sync.port is not set.")
    try:
        waiter: Any = _SerialWaiter(settings.port)
    except Exception as exc:  # noqa: BLE001
        return _keyboard_waiter(
            settings, f"Sync serial port {settings.port!r} failed to open ({exc})."
        )
    return Sync(
        "wait",
        waiter=waiter,
        n_dummy_scans=settings.n_dummy_scans,
        timeout_seconds=settings.timeout_seconds,
        signal=tuple(settings.signal),
    )
