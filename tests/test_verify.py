"""Tests for the bk2-vs-log verifier.

The bk2 reading uses stable-retro internally, which we don't want to
import in unit tests. Instead we monkeypatch ``_count_bk2_steps`` to
return controllable values so the parser/matcher logic is testable
without a ROM or display.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mario_task import verify


def _write_log(p: Path, *entries: str) -> None:
    p.write_text("\n".join(entries) + "\n", encoding="utf-8")


def test_verify_returns_empty_when_logged_matches_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bk2 = tmp_path / "sub-01_run-01_Level1-1_rep-01.bk2"
    bk2.write_bytes(b"\x00")  # any non-empty file; we mock the reader
    log = tmp_path / "session.log"
    _write_log(
        log,
        f"1.000 EXP bk2_start path={bk2} state=Level1-1 frame=1 timer=0.0",
        "1.500 EXP bk2_frame frame=30",
        f"2.000 EXP bk2_end path={bk2} state=Level1-1 frame=60 total_frames=60 timer=1.0 completed=True",
    )
    monkeypatch.setattr(verify, "_count_bk2_steps", lambda _: 60)

    assert verify.verify_log_against_bk2s(log) == []


def test_verify_flags_frame_count_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bk2 = tmp_path / "sub-01_run-01_Level1-1_rep-01.bk2"
    bk2.write_bytes(b"\x00")
    log = tmp_path / "session.log"
    _write_log(
        log,
        f"1.000 EXP bk2_start path={bk2} state=Level1-1 frame=1 timer=0.0",
        f"2.000 EXP bk2_end path={bk2} state=Level1-1 frame=120 total_frames=120 timer=2.0 completed=False",
    )
    monkeypatch.setattr(verify, "_count_bk2_steps", lambda _: 118)

    out = verify.verify_log_against_bk2s(log)
    assert len(out) == 1
    assert out[0].logged_total_frames == 120
    assert out[0].bk2_step_count == 118
    assert "mismatch" in out[0].note


def test_verify_flags_missing_bk2(tmp_path: Path) -> None:
    bk2 = tmp_path / "absent.bk2"
    log = tmp_path / "session.log"
    _write_log(
        log,
        f"1.000 EXP bk2_start path={bk2} state=Level1-1 frame=1 timer=0.0",
        f"2.000 EXP bk2_end path={bk2} state=Level1-1 frame=60 total_frames=60 timer=1.0 completed=True",
    )

    out = verify.verify_log_against_bk2s(log)
    assert len(out) == 1
    assert out[0].bk2_step_count is None
    assert "missing" in out[0].note


def test_verify_skips_interrupted_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bk2_start with no matching bk2_end is unverifiable (run was
    Ctrl+C'd before the engine flushed bk2_end). Don't flag — there's
    nothing to compare."""
    bk2 = tmp_path / "interrupted.bk2"
    bk2.write_bytes(b"\x00")
    log = tmp_path / "session.log"
    _write_log(
        log,
        f"1.000 EXP bk2_start path={bk2} state=Level1-1 frame=1 timer=0.0",
        # No bk2_end line — run was aborted.
    )
    monkeypatch.setattr(verify, "_count_bk2_steps", lambda _: 999)

    assert verify.verify_log_against_bk2s(log) == []


def test_verify_handles_multiple_bk2s_in_one_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bk2a = tmp_path / "a.bk2"
    bk2b = tmp_path / "b.bk2"
    bk2a.write_bytes(b"\x00")
    bk2b.write_bytes(b"\x00")
    log = tmp_path / "session.log"
    _write_log(
        log,
        f"1.000 EXP bk2_start path={bk2a} state=Level1-1 frame=1 timer=0.0",
        f"2.000 EXP bk2_end path={bk2a} state=Level1-1 frame=60 total_frames=60 timer=1.0 completed=True",
        f"3.000 EXP bk2_start path={bk2b} state=Level1-2 frame=1 timer=0.0",
        f"4.000 EXP bk2_end path={bk2b} state=Level1-2 frame=42 total_frames=42 timer=1.0 completed=False",
    )
    counts = {str(bk2a): 60, str(bk2b): 100}
    monkeypatch.setattr(verify, "_count_bk2_steps", lambda p: counts[str(p)])

    out = verify.verify_log_against_bk2s(log)
    assert len(out) == 1
    assert out[0].bk2_path == bk2b
    assert out[0].logged_total_frames == 42
    assert out[0].bk2_step_count == 100


def test_verify_raises_when_log_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        verify.verify_log_against_bk2s(tmp_path / "nope.log")
