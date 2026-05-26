"""Tests for the pure helpers in ``mario_task.gui``.

The actual Dlg rendering needs a display, so the wizard + picker
functions are exercised by the integration smoke test, not here. We
only test ``list_existing_subjects`` and ``format_subject_progress``
because those are pure-stdlib + savestate I/O.

This module imports ``mario_task.gui`` at the top; that module's lazy
import of ``psychopy`` keeps it safe in CI (the actual psychopy import
only happens when the wizard / picker is called).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mario_task import gui, savestate
from mario_task.design import ALL_LEVELS


# ---------------------------------------------------------------------------
# list_existing_subjects
# ---------------------------------------------------------------------------


def test_list_existing_subjects_empty_when_no_sourcedata(tmp_path: Path) -> None:
    assert gui.list_existing_subjects(tmp_path) == []


def test_list_existing_subjects_finds_sub_dirs(tmp_path: Path) -> None:
    for label in ("01", "02", "pilot1"):
        (tmp_path / "sourcedata" / f"sub-{label}").mkdir(parents=True)
    out = gui.list_existing_subjects(tmp_path)
    assert sorted(out) == ["01", "02", "pilot1"]


def test_list_existing_subjects_orders_by_mtime_desc(tmp_path: Path) -> None:
    src = tmp_path / "sourcedata"
    src.mkdir()
    # Create three subject dirs with increasing mtimes.
    for i, label in enumerate(("01", "02", "03")):
        d = src / f"sub-{label}"
        d.mkdir()
        # Bump mtime so ordering is deterministic.
        atime = mtime = time.time() + i
        os_path = str(d)
        import os
        os.utime(os_path, (atime, mtime))
    out = gui.list_existing_subjects(tmp_path)
    # Newest (highest mtime) first.
    assert out[0] == "03"
    assert out[-1] == "01"


def test_list_existing_subjects_ignores_non_sub_dirs(tmp_path: Path) -> None:
    src = tmp_path / "sourcedata"
    src.mkdir()
    (src / "sub-01").mkdir()
    (src / "garbage").mkdir()
    (src / "sub-bad name").mkdir()  # space → rejected by regex
    out = gui.list_existing_subjects(tmp_path)
    assert out == ["01"]


# ---------------------------------------------------------------------------
# format_subject_progress
# ---------------------------------------------------------------------------


def test_format_subject_progress_fresh_subject(tmp_path: Path) -> None:
    (tmp_path / "sourcedata" / "sub-01").mkdir(parents=True)
    assert "fresh" in gui.format_subject_progress(tmp_path, "01")


def test_format_subject_progress_mid_discovery(tmp_path: Path) -> None:
    sub_dir = tmp_path / "sourcedata" / "sub-01"
    sub_dir.mkdir(parents=True)
    # Pretend the subject has just cleared Level1-3 and is heading to 2-1.
    savestate.save(
        sub_dir / "sub-01_phase-discovery_task-mario_savestate.json",
        {"world": 2, "level": 1},
    )
    text = gui.format_subject_progress(tmp_path, "01")
    assert text.startswith("discovery:")
    assert "Level2-1" in text
    # 3 levels cleared (Level1-1, 1-2, 1-3) of 22 total.
    assert "3/22" in text


def test_format_subject_progress_practice(tmp_path: Path) -> None:
    sub_dir = tmp_path / "sourcedata" / "sub-01"
    sub_dir.mkdir(parents=True)
    savestate.save(
        sub_dir / "sub-01_phase-stable_task-mario_savestate.json",
        {"index": 44},
    )
    text = gui.format_subject_progress(tmp_path, "01")
    assert text.startswith("practice:")
    assert "44" in text
    assert "1100" in text  # 50 reps × 22 levels


def test_format_subject_progress_discovery_done(tmp_path: Path) -> None:
    """world≥9 means discovery complete (and a stable savestate should
    have been written too, but if for some reason only discovery exists
    with world=9 we still report it usefully)."""
    sub_dir = tmp_path / "sourcedata" / "sub-01"
    sub_dir.mkdir(parents=True)
    savestate.save(
        sub_dir / "sub-01_phase-discovery_task-mario_savestate.json",
        {"world": 9, "level": 1},
    )
    text = gui.format_subject_progress(tmp_path, "01")
    assert f"{len(ALL_LEVELS)}/22" in text


def test_format_subject_progress_handles_corrupt_savestate(tmp_path: Path) -> None:
    sub_dir = tmp_path / "sourcedata" / "sub-01"
    sub_dir.mkdir(parents=True)
    (sub_dir / "sub-01_phase-discovery_task-mario_savestate.json").write_text("{not json")
    text = gui.format_subject_progress(tmp_path, "01")
    assert "unreadable" in text


# ---------------------------------------------------------------------------
# infer_default_session is a thin re-export — just spot-check it works
# ---------------------------------------------------------------------------


def test_infer_default_session_starts_at_001(tmp_path: Path) -> None:
    assert gui.infer_default_session(tmp_path, "01") == "001"


# ---------------------------------------------------------------------------
# Level-grid helpers in run_config_wizard
# ---------------------------------------------------------------------------


def test_level_field_key_format() -> None:
    assert gui._level_field_key(1, 1) == "level_1_1"
    assert gui._level_field_key(8, 4) == "level_8_4"


def test_collect_enabled_levels_default_set() -> None:
    """Simulate the wizard returning the default 22 levels checked."""
    from mario_task.design import DEFAULT_ENABLED_LEVELS

    data = {
        gui._level_field_key(w, l): ((w, l) in set(DEFAULT_ENABLED_LEVELS))
        for w in range(1, 9)
        for l in range(1, 5)
    }
    enabled = gui._collect_enabled_levels(data)
    assert enabled == tuple(DEFAULT_ENABLED_LEVELS)


def test_collect_enabled_levels_preserves_canonical_ordering() -> None:
    """No matter which boxes are ticked, the result follows (1-1, 1-2, ..., 8-4) order."""
    data = {gui._level_field_key(w, l): False for w in range(1, 9) for l in range(1, 5)}
    # Tick a few out of order; verify they come out sorted.
    data[gui._level_field_key(5, 2)] = True
    data[gui._level_field_key(1, 1)] = True
    data[gui._level_field_key(8, 4)] = True
    enabled = gui._collect_enabled_levels(data)
    assert enabled == ((1, 1), (5, 2), (8, 4))


def test_collect_enabled_levels_empty_when_all_unchecked() -> None:
    data = {gui._level_field_key(w, l): False for w in range(1, 9) for l in range(1, 5)}
    assert gui._collect_enabled_levels(data) == ()
