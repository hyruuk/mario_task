"""Tests for the per-subject design TSV generator.

Pure pandas + stdlib, no display, no psychopy, no retro.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from mario_task import design
from mario_task.design import ALL_LEVELS, N_LEVELS_PER_RUN, generate_design


def test_all_levels_has_22_entries() -> None:
    assert N_LEVELS_PER_RUN == 22
    assert len(set(ALL_LEVELS)) == 22
    # The excluded pairs must not appear.
    assert (2, 2) not in ALL_LEVELS
    assert (7, 2) not in ALL_LEVELS
    # No X-4 castle levels.
    assert all(level != 4 for _, level in ALL_LEVELS)
    # All 8 worlds are represented.
    assert sorted({w for w, _ in ALL_LEVELS}) == list(range(1, 9))


def test_design_is_deterministic_for_a_given_subject() -> None:
    a = generate_design("sub01")
    b = generate_design("sub01")
    pd.testing.assert_frame_equal(a, b)


def test_different_subjects_get_different_designs() -> None:
    a = generate_design("sub01")
    b = generate_design("sub02")
    # They might collide on the first row by chance, but the full 1100-row
    # sequence almost certainly differs. We just need non-equality.
    assert not a.equals(b)


def test_design_length_is_22_times_n_reps() -> None:
    df = generate_design("sub01", n_reps=5)
    assert len(df) == 5 * N_LEVELS_PER_RUN
    df_default = generate_design("sub01")
    assert len(df_default) == design.N_REPS * N_LEVELS_PER_RUN


def test_each_block_of_22_is_a_full_permutation_of_all_levels() -> None:
    df = generate_design("sub01", n_reps=3)
    for block_idx in range(3):
        block = df.iloc[block_idx * 22 : (block_idx + 1) * 22]
        rows_as_tuples = list(zip(block["world"], block["level"]))
        # Same set of levels, just shuffled.
        assert sorted(rows_as_tuples) == sorted(ALL_LEVELS)


def test_design_columns_are_world_and_level() -> None:
    df = generate_design("sub01", n_reps=1)
    assert list(df.columns) == ["world", "level"]
    assert df["world"].dtype.kind == "i"
    assert df["level"].dtype.kind == "i"


def test_ensure_design_creates_file_when_absent(tmp_path: Path) -> None:
    out = design.ensure_design("sub01", tmp_path, n_reps=2)
    assert out == tmp_path / "sub-sub01_design.tsv"
    assert out.exists()
    loaded = pd.read_csv(out, sep="\t")
    pd.testing.assert_frame_equal(loaded, generate_design("sub01", n_reps=2))


def test_ensure_design_does_not_rewrite_existing_file(tmp_path: Path) -> None:
    out = design.ensure_design("sub01", tmp_path, n_reps=2)
    mtime1 = out.stat().st_mtime_ns
    out2 = design.ensure_design("sub01", tmp_path, n_reps=2)
    assert out2 == out
    assert out.stat().st_mtime_ns == mtime1


def test_ensure_design_overwrite_flag_replaces_file(tmp_path: Path) -> None:
    out = design.ensure_design("sub01", tmp_path, n_reps=2)
    # Overwrite with a different rep count → file content (and length) differs.
    design.ensure_design("sub01", tmp_path, n_reps=4, overwrite=True)
    loaded = pd.read_csv(out, sep="\t")
    assert len(loaded) == 4 * N_LEVELS_PER_RUN


def test_design_path_resolves_relative_to_designs_dir(tmp_path: Path) -> None:
    p = design.design_path("sub42", tmp_path)
    assert p.name == "sub-sub42_design.tsv"
    assert p.parent == tmp_path
