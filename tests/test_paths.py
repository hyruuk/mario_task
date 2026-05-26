"""Tests for BIDS path resolution and ROM/state data-root validation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mario_task import paths
from mario_task.design import ALL_LEVELS
from mario_task.paths import BidsPaths, check_data_root


# ---------------------------------------------------------------------------
# BidsPaths
# ---------------------------------------------------------------------------


def _make_paths(tmp_path: Path, *, subject: str = "sub01", session: str = "01") -> BidsPaths:
    return BidsPaths(
        subject=subject,
        session=session,
        output_root=tmp_path / "output",
        timestamp="20260526-153000",
    )


def test_bids_paths_resolves_session_dir(tmp_path: Path) -> None:
    bp = _make_paths(tmp_path)
    assert bp.sourcedata_subject_dir == tmp_path / "output" / "sourcedata" / "sub-sub01"
    assert bp.sourcedata_session_dir == bp.sourcedata_subject_dir / "ses-01"


def test_bids_paths_log_path_matches_bids_convention(tmp_path: Path) -> None:
    bp = _make_paths(tmp_path)
    assert bp.log_path.name == "sub-sub01_ses-01_20260526-153000.log"
    assert bp.log_path.parent == bp.sourcedata_session_dir


def test_bids_paths_events_tsv(tmp_path: Path) -> None:
    bp = _make_paths(tmp_path)
    p = bp.events_tsv("task-mario_phase-discovery_run-01")
    assert p.name == (
        "sub-sub01_ses-01_20260526-153000_task-mario_phase-discovery_run-01_events.tsv"
    )
    assert p.parent == bp.sourcedata_session_dir


def test_bids_paths_movie_path_no_counter(tmp_path: Path) -> None:
    bp = _make_paths(tmp_path)
    p = bp.movie_path(
        task_name="task-mario_run-01",
        game_name="SuperMarioBros-Nes",
        state_name="Level1-1",
    )
    assert p.name.endswith(".bk2")
    assert "SuperMarioBros-Nes" in p.name
    assert "Level1-1" in p.name
    # No counter suffix when counter == 0.
    assert "_000.bk2" not in p.name


def test_bids_paths_movie_path_with_counter(tmp_path: Path) -> None:
    bp = _make_paths(tmp_path)
    p = bp.movie_path(
        task_name="task-mario_run-01",
        game_name="SuperMarioBros-Nes",
        state_name="Level1-1",
        counter=3,
    )
    assert p.name.endswith("_003.bk2")


def test_bids_paths_savestate(tmp_path: Path) -> None:
    bp = _make_paths(tmp_path)
    d = bp.savestate("discovery")
    s = bp.savestate("stable")
    assert d.name == "sub-sub01_phase-discovery_task-mario_savestate.json"
    assert s.name == "sub-sub01_phase-stable_task-mario_savestate.json"
    # Savestates live at the per-subject level, NOT inside ses-YY.
    assert d.parent == bp.sourcedata_subject_dir
    assert s.parent == bp.sourcedata_subject_dir


def test_bids_paths_savestate_rejects_unknown_phase(tmp_path: Path) -> None:
    bp = _make_paths(tmp_path)
    with pytest.raises(ValueError):
        bp.savestate("warmup")  # type: ignore[arg-type]


def test_bids_paths_design_tsv(tmp_path: Path) -> None:
    bp = _make_paths(tmp_path)
    assert bp.design_tsv.name == "sub-sub01_design.tsv"


def test_subject_label_validation_rejects_path_separator() -> None:
    with pytest.raises(ValueError):
        BidsPaths(subject="../evil", session="01", output_root=Path("/tmp/x"))


def test_subject_label_validation_rejects_empty() -> None:
    with pytest.raises(ValueError):
        BidsPaths(subject="", session="01", output_root=Path("/tmp/x"))


def test_subject_label_validation_accepts_alnum_dash_underscore() -> None:
    BidsPaths(subject="sub01", session="01", output_root=Path("/tmp/x"))
    BidsPaths(subject="A1-B2_C3", session="ses_001", output_root=Path("/tmp/x"))


def test_events_tsv_rejects_path_traversal(tmp_path: Path) -> None:
    bp = _make_paths(tmp_path)
    for bad in ("../escape", "task/with/slash", "task\\with\\backslash", ".hidden"):
        with pytest.raises(ValueError):
            bp.events_tsv(bad)


def test_make_timestamp_matches_format() -> None:
    ts = paths.make_timestamp()
    assert re.fullmatch(r"\d{8}-\d{6}", ts)


# ---------------------------------------------------------------------------
# check_data_root
# ---------------------------------------------------------------------------


def _create_complete_data_root(root: Path) -> Path:
    """Create a full valid SuperMarioBros-Nes data root under ``root``."""
    smb = root / "SuperMarioBros-Nes"
    smb.mkdir(parents=True)
    (smb / "rom.nes").write_bytes(b"\x4e\x45\x53\x1a" + b"\x00" * 40972)  # iNES header + padding
    (smb / "data.json").write_text("{}")
    (smb / "scenario.json").write_text("{}")
    for world, level in ALL_LEVELS:
        (smb / f"Level{world}-{level}.state").write_bytes(b"state-bytes")
    return smb


def test_check_data_root_returns_none_for_complete_layout(tmp_path: Path) -> None:
    smb = _create_complete_data_root(tmp_path)
    assert check_data_root(smb) is None


def test_check_data_root_complains_when_directory_missing(tmp_path: Path) -> None:
    err = check_data_root(tmp_path / "nope")
    assert err is not None
    assert "not found" in err
    assert "setup_env.sh" in err  # actionable hint


def test_check_data_root_complains_when_rom_empty(tmp_path: Path) -> None:
    smb = _create_complete_data_root(tmp_path)
    (smb / "rom.nes").write_bytes(b"")  # empty / dangling
    err = check_data_root(smb)
    assert err is not None
    assert "rom.nes" in err
    assert "datalad get" in err


def test_check_data_root_complains_when_states_missing(tmp_path: Path) -> None:
    smb = _create_complete_data_root(tmp_path)
    (smb / "Level5-2.state").unlink()
    err = check_data_root(smb)
    assert err is not None
    assert "Level5-2.state" in err


def test_check_data_root_complains_when_scenario_missing(tmp_path: Path) -> None:
    smb = _create_complete_data_root(tmp_path)
    (smb / "scenario.json").unlink()
    err = check_data_root(smb)
    assert err is not None
    assert "scenario.json" in err


def test_check_data_root_complains_when_state_empty(tmp_path: Path) -> None:
    """Dangling git-annex pointer = symlink that resolves to a 0-byte file."""
    smb = _create_complete_data_root(tmp_path)
    (smb / "Level1-1.state").write_bytes(b"")
    err = check_data_root(smb)
    assert err is not None
    assert "Level1-1.state" in err
