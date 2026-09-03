"""Covers run-start synchronisation: send, wait, and the degrade-never-crash path.

The generators are driven manually so no window or scanner is needed. The
suite is the one that ships with ``controller_validation_task``: the two tasks
share this module's behaviour, so they share its tests.
"""

from __future__ import annotations

import logging
import time

import pytest

from mario_task import markers, sync
from mario_task.settings import SyncSettings


@pytest.fixture(autouse=True)
def reset_markers():
    markers._reset_for_tests()
    yield
    markers._reset_for_tests()


def drive(generator):
    """Exhaust a frame generator and return (n_frames, return_value)."""
    frames = 0
    try:
        while True:
            next(generator)
            frames += 1
            if frames > 10000:  # pragma: no cover - guards a hung test
                raise AssertionError("generator did not terminate")
    except StopIteration as stop:
        return frames, stop.value


class FakeSender:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, value):
        self.sent.append(value)

    def close(self):
        self.closed = True


class FakeWaiter:
    """Yields a scripted number of TTLs per poll.

    ``poll_delay`` simulates the wall-clock cost of a frame. The real waiter is
    driven once per refresh (~16 ms); the test driver spins with no delay, so a
    timeout test needs this to make time actually pass.
    """

    def __init__(self, script, poll_delay=0.0):
        self.script = list(script)
        self.poll_delay = poll_delay
        self.primed = False
        self.closed = False

    def prime(self):
        self.primed = True

    def poll(self):
        if self.poll_delay:
            time.sleep(self.poll_delay)
        return self.script.pop(0) if self.script else 0

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


def test_single_character_becomes_its_ordinal():
    assert sync._as_byte("s") == 115


def test_numeric_string_is_used_directly():
    assert sync._as_byte("255") == 255
    assert sync._as_byte(7) == 7


def test_multi_character_value_is_rejected():
    # Silently sending only the first character would be a nasty surprise.
    with pytest.raises(ValueError, match="single character"):
        sync._as_byte("start")


# ---------------------------------------------------------------------------
# mode = none
# ---------------------------------------------------------------------------


def test_none_mode_starts_immediately():
    s = sync.configure(SyncSettings(mode="none"))
    frames, _ = drive(s.start())
    assert frames == 0
    assert s.sync_time is not None
    assert not s.waits


# ---------------------------------------------------------------------------
# mode = send
# ---------------------------------------------------------------------------


def test_send_mode_emits_the_value_once():
    sender = FakeSender()
    s = sync.Sync("send", sender=sender, value=115)
    frames, _ = drive(s.start())
    assert frames == 0        # sending never blocks
    assert sender.sent == [115]


def test_send_mode_reuses_the_marker_backend():
    markers.configure(backend="null")
    s = sync.configure(SyncSettings(mode="send", backend="markers", signal=("s",)))
    drive(s.start())
    # It grabbed the configured backend rather than opening its own port.
    assert isinstance(s._sender, sync._MarkerSender)
    assert s._sender._backend is markers.get_backend()


def test_send_failure_does_not_abort_the_run(caplog):
    class Exploding:
        def send(self, value):
            raise OSError("cable unplugged")

        def close(self):
            pass

    s = sync.Sync("send", sender=Exploding(), value=1)
    with caplog.at_level(logging.WARNING):
        drive(s.start())
    assert "failed to send" in caplog.text


def test_an_unsendable_signal_degrades_instead_of_raising(caplog):
    with caplog.at_level(logging.WARNING):
        s = sync.configure(SyncSettings(mode="send", backend="markers", signal=("hello",)))
    assert isinstance(s._sender, sync._NullSender)
    assert "single character" in caplog.text
    drive(s.start())            # and the run still starts


# ---------------------------------------------------------------------------
# mode = wait
# ---------------------------------------------------------------------------


def test_wait_returns_on_the_first_ttl():
    waiter = FakeWaiter([0, 0, 1])
    s = sync.Sync("wait", waiter=waiter)
    frames, _ = drive(s.start())
    assert waiter.primed
    assert frames == 2       # yielded twice, returned on the third poll
    assert s.waits


def test_wait_discards_dummy_scans():
    waiter = FakeWaiter([1, 1, 1])
    s = sync.Sync("wait", waiter=waiter, n_dummy_scans=2)
    drive(s.start())
    # 2 dummies + the real one = 3 TTLs consumed.
    assert waiter.script == []


def test_wait_counts_multiple_ttls_in_one_poll():
    waiter = FakeWaiter([3])
    s = sync.Sync("wait", waiter=waiter, n_dummy_scans=2)
    frames, _ = drive(s.start())
    assert frames == 0


def test_wait_times_out_and_starts_anyway(caplog):
    waiter = FakeWaiter([], poll_delay=0.002)  # a scanner that never fires
    s = sync.Sync("wait", waiter=waiter, timeout_seconds=0.02)
    with caplog.at_level(logging.WARNING):
        drive(s.start())
    assert "No scanner trigger" in caplog.text
    assert s.sync_time is not None


def test_wait_with_a_bad_serial_port_falls_back_to_the_keyboard(caplog):
    with caplog.at_level(logging.WARNING):
        s = sync.configure(
            SyncSettings(mode="wait", backend="serial", port="/dev/definitely-not-a-port")
        )
    assert s.mode == "wait"
    assert s.signal == SyncSettings().signal


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def test_close_releases_the_sender_and_is_idempotent():
    sender = FakeSender()
    s = sync.Sync("send", sender=sender, value=1)
    s.close()
    s.close()
    assert sender.closed


def test_close_does_not_close_a_borrowed_marker_backend():
    markers.configure(backend="null")
    s = sync.configure(SyncSettings(mode="send", backend="markers", signal=("s",)))
    s.close()
    # markers.close() owns that backend, not sync.
    assert markers.get_backend() is not None


# ---------------------------------------------------------------------------
# backend = "none", and the degraded paths that land on it
#
# The rule: a transport that is missing or will not open behaves as if the
# backend were "none" — `wait` listens on the keyboard, `send` warns and starts
# the run anyway. Never abort the session over a cable.
# ---------------------------------------------------------------------------


def test_wait_on_the_default_backend_listens_on_the_keyboard():
    s = sync.configure(SyncSettings(mode="wait"))
    assert SyncSettings().backend == "none"
    assert SyncSettings().signal == ("s",)
    assert s.waits
    assert s.signal == SyncSettings().signal
    assert isinstance(s._waiter, sync._KeyboardWaiter)


def test_wait_on_backend_none_is_not_a_fallback():
    # Configured that way on purpose, so nothing to explain.
    s = sync.configure(SyncSettings(mode="wait", backend="none"))
    assert s.fallback_reason == ""


def test_wait_mode_missing_serial_port_falls_back_to_the_keyboard(caplog):
    with caplog.at_level(logging.WARNING):
        s = sync.configure(SyncSettings(mode="wait", backend="serial", port=None))
    assert s.waits
    assert s.signal == SyncSettings().signal
    assert "sync.port is not set" in caplog.text
    assert "sync.port is not set" in s.fallback_reason


def test_wait_mode_bad_serial_port_falls_back_to_the_keyboard():
    s = sync.configure(
        SyncSettings(mode="wait", backend="serial", port="/dev/definitely-not-a-port")
    )
    assert s.waits
    assert isinstance(s._waiter, sync._KeyboardWaiter)


def test_keyboard_fallback_actually_waits():
    s = sync.configure(SyncSettings(mode="wait", backend="serial", port=None))
    s._waiter = FakeWaiter([0, 0, 1])   # scripted "TTL on the third poll"
    frames, _ = drive(s.start())
    assert frames == 2
    assert s.sync_time is not None


def test_waiting_message_says_nothing_about_the_keys():
    # Participant-facing screen. Which key we watch is operator information.
    s = sync.configure(SyncSettings(mode="wait"))
    assert s._waiting_message() == "Waiting for the scanner"


def test_send_on_backend_none_starts_the_run_anyway(caplog):
    with caplog.at_level(logging.WARNING):
        s = sync.configure(SyncSettings(mode="send", backend="none", signal=("s",)))
    assert not s.waits
    frames, _ = drive(s.start())
    assert frames == 0                      # never blocks
    assert s.sync_time is not None
    assert "nothing to send the start signal to" in caplog.text


def test_a_dropped_signal_is_not_logged_as_sent(caplog):
    s = sync.configure(SyncSettings(mode="send", backend="none"))
    with caplog.at_level(logging.INFO):
        drive(s.start())
    assert "Start signal dropped" in caplog.text
    assert "Start signal sent" not in caplog.text


def test_send_with_a_missing_port_starts_the_run_anyway(caplog):
    with caplog.at_level(logging.WARNING):
        s = sync.configure(
            SyncSettings(mode="send", backend="serial", port=None, signal=("s",))
        )
    assert not s.waits
    drive(s.start())                        # the _NullSender logs, nothing raises
    assert "sync.port is not set" in caplog.text


def test_send_with_a_missing_parallel_port_also_degrades():
    s = sync.configure(
        SyncSettings(mode="send", backend="parallel", port=None, signal=("s",))
    )
    assert isinstance(s._sender, sync._NullSender)


def test_send_with_a_bad_port_degrades(caplog):
    with caplog.at_level(logging.WARNING):
        s = sync.configure(
            SyncSettings(mode="send", backend="serial", port="/dev/definitely-not-a-port")
        )
    assert isinstance(s._sender, sync._NullSender)
    assert "failed to open" in caplog.text


def test_a_configured_port_still_sends():
    # The degradation must not fire when the transport is fine.
    sender = FakeSender()
    s = sync.Sync("send", sender=sender, value=115)
    drive(s.start())
    assert sender.sent == [115]
    assert s.mode == "send"


def test_markers_backend_needs_no_port_and_does_not_degrade():
    markers.configure(backend="null")
    s = sync.configure(SyncSettings(mode="send", backend="markers", port=None, signal=("s",)))
    assert s.mode == "send"
    assert not isinstance(s._sender, sync._NullSender)


# ---------------------------------------------------------------------------
# Where sync sits in a run
#
# Imports mario_task.session, which pulls in psychopy and retro — hence the
# skip when they are unavailable. Everything above this point stays pure.
# ---------------------------------------------------------------------------


session = pytest.importorskip("mario_task.session", reason="needs psychopy + retro")


class RecordingTask:
    """Stub task whose phases record the order they were driven in."""

    def __init__(self, order):
        self._order = order

    def instructions(self, exp_win, ctl_win):
        self._order.append("instructions")
        yield

    def run(self, exp_win, ctl_win):
        self._order.append("run")
        yield

    def stop(self, exp_win, ctl_win):
        self._order.append("stop")
        yield

    def save(self):
        self._order.append("save")


class RecordingWaiter(FakeWaiter):
    """A waiter that notes when it was primed, in a shared order list."""

    def __init__(self, order, script):
        super().__init__(script)
        self._order = order

    def prime(self):
        super().prime()
        self._order.append("sync")


def test_waiting_for_the_scanner_replaces_the_ready_prompt():
    """In wait mode the trigger is the go signal, so it is the only screen.

    Showing "press X when ready" as well would put a second gate on a run the
    scanner has already released — the subject would sit in a live sequence
    waiting to be told to press something.
    """
    order: list[str] = []
    s = sync.Sync("wait", waiter=RecordingWaiter(order, [0, 1]))
    session._run_task(RecordingTask(order), None, use_eeg=False, sync_obj=s)
    assert order == ["sync", "run", "stop", "save"]
    assert "instructions" not in order
    assert s.sync_time is not None


def test_send_mode_prompts_first_then_starts_the_recording():
    """The other order, for the same reason: don't leave a scanner running.

    Here we are the go signal, so the subject confirms they are ready and only
    then does the start pulse go out.
    """
    order: list[str] = []
    sender = FakeSender()
    s = sync.Sync("send", sender=sender, value=115)
    session._run_task(RecordingTask(order), None, use_eeg=False, sync_obj=s)
    assert order == ["instructions", "run", "stop", "save"]
    assert sender.sent == [115]


def test_none_mode_keeps_the_ready_prompt_as_the_only_gate():
    """The desk case: no scanner, so the subject starts the run."""
    order: list[str] = []
    s = sync.Sync("none")
    session._run_task(RecordingTask(order), None, use_eeg=False, sync_obj=s)
    assert order == ["instructions", "run", "stop", "save"]


def test_quitting_during_the_wait_never_reaches_the_run(monkeypatch):
    """Ctrl+Q on the waiting screen aborts the run, it does not fall through."""
    order: list[str] = []
    # A scanner that never fires, and an operator who gives up on frame 1.
    monkeypatch.setattr(session, "_listen_shortcuts", lambda: "q")
    s = sync.Sync("wait", waiter=RecordingWaiter(order, []))
    shortcut = session._run_task(
        RecordingTask(order), None, use_eeg=False, sync_obj=s
    )
    assert shortcut == "q"
    assert "run" not in order


def test_a_task_with_no_sync_object_goes_straight_to_the_prompt():
    """What the end-of-run prompt gets: nothing to align, so nothing to wait for."""
    order: list[str] = []
    session._run_task(RecordingTask(order), None, use_eeg=False, sync_obj=None)
    assert order == ["instructions", "run", "stop", "save"]
