"""Atomic JSON read/write for cross-session progress files.

A "savestate" is a small JSON blob that captures how far a subject has
progressed within a phase. Two are used:

    output/sourcedata/sub-XX/sub-XX_phase-discovery_task-mario_savestate.json
        {"world": 1, "level": 1}    # next level to play in discovery
    output/sourcedata/sub-XX/sub-XX_phase-stable_task-mario_savestate.json
        {"index": 0}                # next row in the design TSV to play in practice

Writes are atomic: we write to ``<path>.tmp``, ``fsync`` it, then
``os.replace`` to the final name. ``os.replace`` is atomic on POSIX, so a
Ctrl+C between bytes can never leave a half-written file behind.

The module is stdlib-only on purpose: it's part of the pure-Python core and
must be importable without psychopy or stable-retro installed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def load(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load a savestate JSON.

    Raises ``FileNotFoundError`` if the file is absent and
    ``json.JSONDecodeError`` if the contents are not valid JSON.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"savestate at {path!s} must be a JSON object, got {type(data).__name__}")
    return data


def load_or_default(
    path: str | os.PathLike[str], default: dict[str, Any]
) -> dict[str, Any]:
    """Load a savestate if it exists, otherwise return a *copy* of ``default``."""
    try:
        return load(path)
    except FileNotFoundError:
        return dict(default)


def save(path: str | os.PathLike[str], data: dict[str, Any]) -> None:
    """Atomically write ``data`` to ``path`` as JSON.

    The parent directory must already exist (we don't auto-create it — that
    would mask bugs where the BIDS path resolver was misconfigured).
    """
    p = Path(path)
    if not p.parent.exists():
        raise FileNotFoundError(
            f"Parent directory {p.parent!s} does not exist; "
            f"create it before calling savestate.save()."
        )
    tmp = p.with_suffix(p.suffix + ".tmp")
    # Write + fsync to make sure the bytes are on disk before we rename.
    with open(tmp, "w", encoding="utf-8") as f:
        # ensure_ascii=False keeps unicode characters readable in the on-disk
        # file (e.g. "world — paused"), at no functional cost since we always
        # encode as UTF-8.
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    # os.replace is atomic on POSIX.
    os.replace(tmp, p)
