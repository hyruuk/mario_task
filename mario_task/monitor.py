"""Live LSL marker monitor.

Run this in a SEPARATE terminal while the experiment is running. It
subscribes to the experiment's LSL outlet and prints every marker with
its LSL timestamp + decoded meaning.

::

    # Default stream name (matches the wizard default):
    python -m mario_task.monitor

    # Different stream name (matches whatever you set in config.json):
    python -m mario_task.monitor --stream my_other_stream

    # Hide the 60 Hz gameplay heartbeat (only show lifecycle markers):
    python -m mario_task.monitor --quiet

    # Show rolling stats (markers/sec) every 5 s:
    python -m mario_task.monitor --stats

Ctrl+C to quit. Exit code 0 on clean shutdown, 1 if the stream couldn't
be resolved within the timeout.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from typing import Iterable

from pathlib import Path

from mario_task import markers, settings
from mario_task.markers import decode_marker


def _load_codes_from_config(config_path: Path) -> None:
    """If ``config.json`` exists at ``config_path``, apply its trigger
    codes to this process so :func:`decode_marker` labels match what
    the experiment is publishing.

    Without this step the monitor would always show the markers using
    the *default* code scheme — which silently mislabels markers when
    the experiment uses custom codes (e.g. a different ``game_frame_mod``).
    """
    if not config_path.is_file():
        print(
            f"(no config.json at {config_path}; decoding with defaults)",
            file=sys.stderr,
        )
        return
    try:
        s = settings.load_from_file(config_path)
    except Exception as exc:  # noqa: BLE001 — never crash the monitor on a bad config
        print(
            f"(failed to load {config_path}: {exc}; decoding with defaults)",
            file=sys.stderr,
        )
        return
    markers.set_codes(s.triggers.codes)
    c = s.triggers.codes
    print(
        f"(loaded codes from {config_path}: "
        f"lifecycle={c.task_start},{c.task_stop},{c.game_reset},{c.non_game_flip} "
        f"gameplay={c.game_frame_base}+%{c.game_frame_mod})",
        file=sys.stderr,
    )


def _resolve_outlet(stream_name: str, timeout: float):
    """Resolve a single LSL outlet by ``name``; raise SystemExit if not found."""
    import pylsl

    print(f"Resolving LSL stream {stream_name!r} (timeout {timeout:g}s)...",
          file=sys.stderr, flush=True)
    streams = pylsl.resolve_byprop("name", stream_name, minimum=1, timeout=timeout)
    if not streams:
        raise SystemExit(
            f"\nNo LSL stream named {stream_name!r} found within {timeout:g}s.\n"
            f"  - Is the experiment running?\n"
            f"  - Is your config.json (or LSL_STREAM_NAME env var) using a "
            f"different name? Current default is 'mario_task'.\n"
            f"  - On Linux, ensure pylsl can reach the LSL multicast network."
        )
    info = streams[0]
    print(
        f"Connected: name={info.name()!r} type={info.type()!r} "
        f"source={info.source_id()!r}",
        file=sys.stderr, flush=True,
    )
    return pylsl.StreamInlet(streams[0])


def _format_header() -> str:
    return f"{'lsl_time':>16}   {'rel_s':>8}   {'value':>5}   meaning"


def _format_row(ts: float, rel: float, value: int) -> str:
    return f"{ts:16.6f}   {rel:8.3f}   {value:>5d}   {decode_marker(value)}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="mario_task.monitor",
        description="Live LSL marker monitor for mario_task.",
    )
    p.add_argument(
        "--stream",
        default=os.environ.get("LSL_STREAM_NAME", "mario_task"),
        help="LSL stream name to subscribe to (default: 'mario_task', or $LSL_STREAM_NAME).",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for the stream to appear (default: 10).",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Hide GAME_FRAME (60 Hz heartbeat); only show lifecycle markers.",
    )
    p.add_argument(
        "--stats",
        action="store_true",
        help="Print rolling per-code counts every 5 s.",
    )
    p.add_argument(
        "--config",
        default="config.json",
        help=(
            "Path to the experiment's config.json (default: ./config.json). "
            "Used so the monitor's decoding matches the experiment's "
            "code scheme. Pass '/dev/null' or a non-existent path to "
            "force defaults."
        ),
    )
    args = p.parse_args(argv)

    _load_codes_from_config(Path(args.config))
    inlet = _resolve_outlet(args.stream, args.timeout)

    print(_format_header(), file=sys.stderr, flush=True)
    print("─" * 70, file=sys.stderr, flush=True)

    start_lsl_ts: float | None = None
    counts: Counter[str] = Counter()
    last_stats_time = time.monotonic()

    try:
        while True:
            sample, ts = inlet.pull_sample(timeout=1.0)
            if sample is None:
                # No data this second. If stats are on, dump them now so
                # the operator sees the dead air.
                if args.stats and (time.monotonic() - last_stats_time) > 5.0:
                    print(f"[stats] {dict(counts)}", file=sys.stderr, flush=True)
                    counts.clear()
                    last_stats_time = time.monotonic()
                continue
            value = int(sample[0])
            label = decode_marker(value)
            counts[label.split("[")[0]] += 1  # collapse GAME_FRAME[...] variations

            if start_lsl_ts is None:
                start_lsl_ts = ts
            rel = ts - start_lsl_ts

            if args.quiet and label.startswith("GAME_FRAME"):
                pass  # suppress per-frame chatter
            else:
                print(_format_row(ts, rel, value), flush=True)

            if args.stats and (time.monotonic() - last_stats_time) > 5.0:
                print(f"[stats] {dict(counts)}", file=sys.stderr, flush=True)
                counts.clear()
                last_stats_time = time.monotonic()
    except KeyboardInterrupt:
        print("\nMonitor stopped.", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
