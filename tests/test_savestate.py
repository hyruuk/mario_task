"""Tests for the atomic-savestate module.

All tests are pure-Python: no display, no psychopy, no retro. They must
remain runnable on a fresh box with only the `dev` extras installed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mario_task import savestate


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "sub-99_phase-discovery_task-mario_savestate.json"
    payload = {"world": 3, "level": 2}

    savestate.save(p, payload)

    assert p.exists()
    assert savestate.load(p) == payload


def test_save_overwrites_atomically(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    savestate.save(p, {"world": 1, "level": 1})
    savestate.save(p, {"world": 1, "level": 2})

    assert savestate.load(p) == {"world": 1, "level": 2}
    # The .tmp sibling must not linger after a successful save.
    assert not p.with_suffix(p.suffix + ".tmp").exists()


def test_save_refuses_missing_parent_directory(tmp_path: Path) -> None:
    p = tmp_path / "does" / "not" / "exist" / "state.json"
    with pytest.raises(FileNotFoundError):
        savestate.save(p, {"world": 1, "level": 1})


def test_load_or_default_returns_default_when_file_absent(tmp_path: Path) -> None:
    p = tmp_path / "absent.json"
    default = {"world": 1, "level": 1}

    out = savestate.load_or_default(p, default)

    assert out == default
    # Must be a copy: mutating the result must not mutate the default.
    out["world"] = 99
    assert default == {"world": 1, "level": 1}


def test_load_or_default_returns_loaded_when_file_present(tmp_path: Path) -> None:
    p = tmp_path / "present.json"
    savestate.save(p, {"world": 5, "level": 3})

    assert savestate.load_or_default(p, {"world": 1, "level": 1}) == {"world": 5, "level": 3}


def test_load_raises_on_non_object_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("[1, 2, 3]")  # valid JSON but not a dict

    with pytest.raises(ValueError):
        savestate.load(p)


def test_load_raises_on_corrupt_json(tmp_path: Path) -> None:
    p = tmp_path / "corrupt.json"
    p.write_text("{not json}")

    with pytest.raises(json.JSONDecodeError):
        savestate.load(p)


def test_save_uses_unicode(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    savestate.save(p, {"note": "discovery — paused at world 3"})

    # Verify the raw bytes round-trip too, not just the parsed dict.
    raw = p.read_text(encoding="utf-8")
    assert "discovery — paused at world 3" in raw


def test_simulated_crash_between_tmp_write_and_replace_leaves_original_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the process dies after we wrote ``state.json.tmp`` but before
    ``os.replace`` ran, the original ``state.json`` must still be readable
    and unchanged.

    We simulate this by monkeypatching ``os.replace`` to raise. The atomic
    contract guarantees the original file is the one observers will see.
    """
    p = tmp_path / "state.json"
    savestate.save(p, {"world": 1, "level": 1})
    original_mtime = p.stat().st_mtime_ns

    def boom(*args, **kwargs):  # noqa: ANN001 - test-only stub
        raise KeyboardInterrupt("simulated Ctrl+C between fsync and replace")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(KeyboardInterrupt):
        savestate.save(p, {"world": 99, "level": 99})

    # Original file is unchanged.
    assert savestate.load(p) == {"world": 1, "level": 1}
    assert p.stat().st_mtime_ns == original_mtime
    # The .tmp file remains on disk (we didn't get to replace it).
    # On a real crash this would be cleaned up at next save() — verify.
    assert p.with_suffix(p.suffix + ".tmp").exists()

    # Next successful save replaces it cleanly.
    monkeypatch.undo()
    savestate.save(p, {"world": 2, "level": 2})
    assert savestate.load(p) == {"world": 2, "level": 2}
    assert not p.with_suffix(p.suffix + ".tmp").exists()
