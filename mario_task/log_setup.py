"""PsychoPy LogFile lifecycle + flush policy.

The upstream codebase had a subtle bug: ``logging.LogFile(...)`` was
called as a bare expression, so the returned object was eligible for
garbage collection as soon as the call site returned. It worked in
practice because PsychoPy internally held a reference, but that's
fragile and not part of psychopy's public contract.

We fix that by returning the ``LogFile`` from :func:`create_session_log`
and requiring the caller to hold on to it for the duration of the
session. Concretely, ``session.run_session`` assigns it to a local
that lives for the whole function scope.
"""

from __future__ import annotations

from pathlib import Path

from psychopy import core, logging


# Initialize the global PsychoPy clock once per process so all log lines
# share a single time reference (otherwise frame-count drift accumulates).
_GLOBAL_CLOCK = core.MonotonicClock(0)
logging.setDefaultClock(_GLOBAL_CLOCK)


def create_session_log(log_path: str | Path, level: int = logging.INFO) -> logging.LogFile:
    """Open a per-session psychopy LogFile and return it.

    Hold the returned object for the duration of the session — once the
    last reference is dropped, the underlying file handle may be closed
    by GC and subsequent log lines vanish.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return logging.LogFile(str(log_path), level=level, filemode="w")


def flush() -> None:
    """Force PsychoPy to flush its buffered log lines to disk.

    Call this every ~60 frames during the run loop so a hard crash
    doesn't drop the last second of telemetry.
    """
    logging.flush()
