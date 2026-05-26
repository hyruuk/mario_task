"""Verify that on-disk bk2 recordings match the timing log.

The engine writes three categories of line to the psychopy ``LogFile``:

    bk2_start path=<absolute>  state=<level>  frame=1    timer=<float>
    bk2_frame                                frame=<N>
    bk2_end   path=<absolute>  state=<level>  frame=<N>  total_frames=<N>  ...

For every ``bk2_start`` / ``bk2_end`` pair the recorded ``total_frames``
must equal the number of frames that stable-retro's ``Movie`` reader can
step through for that bk2 file. If they don't match, the bk2 was
truncated (Ctrl+C mid-write, crash, ...) or the log lost lines.

This module exposes a single public function :func:`verify_log_against_bk2s`
that returns a list of mismatches (each a ``Mismatch`` dataclass) — or an
empty list if everything is consistent. A small CLI is also provided::

    python -m mario_task.verify output/sourcedata/sub-01/ses-001/sub-01_ses-001_*.log
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Line formats are stable — see engine.run_emulator.
_RX_START = re.compile(
    r"bk2_start\s+path=(?P<path>\S+)\s+state=(?P<state>\S+)\s+frame=(?P<frame>\d+)"
)
_RX_END = re.compile(
    r"bk2_end\s+path=(?P<path>\S+)\s+state=(?P<state>\S+)\s+frame=(?P<frame>\d+)\s+"
    r"total_frames=(?P<total>\d+)"
)


@dataclass
class Mismatch:
    """One discrepancy between the log's record and the bk2 on disk."""

    bk2_path: Path
    state: str
    logged_total_frames: int
    bk2_step_count: int | None  # None if the file is missing / unreadable
    note: str

    def __str__(self) -> str:
        return (
            f"{self.bk2_path.name}: "
            f"logged={self.logged_total_frames} bk2={self.bk2_step_count} ({self.note})"
        )


def _parse_log(log_path: Path) -> list[tuple[Path, str, int]]:
    """Return one ``(bk2_path, state_name, logged_total_frames)`` per bk2 in the log.

    Pairs each ``bk2_start`` with its matching ``bk2_end`` by path. If a
    start has no matching end (run was interrupted before the engine
    flushed bk2_end), that pair is skipped — verification has no
    expected-frame-count to compare against.
    """
    text = log_path.read_text(encoding="utf-8", errors="replace")
    ends_by_path: dict[str, int] = {}
    for m in _RX_END.finditer(text):
        ends_by_path[m.group("path")] = int(m.group("total"))

    pairs: list[tuple[Path, str, int]] = []
    seen_paths: set[str] = set()
    for m in _RX_START.finditer(text):
        path = m.group("path")
        if path in seen_paths:
            continue
        seen_paths.add(path)
        total = ends_by_path.get(path)
        if total is None:
            continue  # interrupted; nothing to verify
        pairs.append((Path(path), m.group("state"), total))
    return pairs


def _count_bk2_steps(bk2_path: Path) -> int:
    """Count the number of action-frames recorded in a bk2 file.

    Uses stable-retro's ``Movie`` reader which exposes ``step()`` returning
    True while more frames remain. Each step is one ``emulator.step()`` —
    matches what we count in the engine loop.
    """
    import retro  # local import: keeps verify importable without retro

    movie = retro.Movie(str(bk2_path))
    count = 0
    while movie.step():
        count += 1
    movie.close()
    return count


def verify_log_against_bk2s(log_path: str | Path) -> list[Mismatch]:
    """Parse ``log_path`` and verify every recorded bk2 frame count.

    Returns a list of :class:`Mismatch` describing every discrepancy.
    An empty list means everything checks out.
    """
    log_path = Path(log_path)
    if not log_path.is_file():
        raise FileNotFoundError(f"log file not found: {log_path}")

    mismatches: list[Mismatch] = []
    for bk2_path, state, logged_total in _parse_log(log_path):
        if not bk2_path.is_file():
            mismatches.append(Mismatch(
                bk2_path=bk2_path,
                state=state,
                logged_total_frames=logged_total,
                bk2_step_count=None,
                note="bk2 file missing on disk",
            ))
            continue
        try:
            bk2_steps = _count_bk2_steps(bk2_path)
        except Exception as exc:  # noqa: BLE001 — surface any retro errors verbatim
            mismatches.append(Mismatch(
                bk2_path=bk2_path,
                state=state,
                logged_total_frames=logged_total,
                bk2_step_count=None,
                note=f"failed to read bk2: {exc}",
            ))
            continue
        if bk2_steps != logged_total:
            mismatches.append(Mismatch(
                bk2_path=bk2_path,
                state=state,
                logged_total_frames=logged_total,
                bk2_step_count=bk2_steps,
                note=f"frame-count mismatch (delta={bk2_steps - logged_total})",
            ))
    return mismatches


def _main(argv: list[str]) -> int:
    if not argv:
        print("Usage: python -m mario_task.verify <log_path> [<log_path> ...]", file=sys.stderr)
        return 2
    any_mismatch = False
    for arg in argv:
        for path in sorted(Path().glob(arg)) or [Path(arg)]:
            try:
                mismatches = verify_log_against_bk2s(path)
            except FileNotFoundError as exc:
                print(f"[skip] {exc}", file=sys.stderr)
                continue
            if mismatches:
                any_mismatch = True
                print(f"[FAIL] {path}: {len(mismatches)} mismatch(es)")
                for m in mismatches:
                    print(f"  {m}")
            else:
                print(f"[ok]   {path}")
    return 1 if any_mismatch else 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
