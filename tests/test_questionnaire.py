"""Tests for the subject-level cumulative questionnaire TSV writer.

We only test the file-writing helper (``_append_to_subject_tsv``) — the
``run`` generator needs psychopy + a display, so it's exercised by the
integration smoke test instead.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from mario_task import questionnaire
from mario_task.questionnaire import _SUBJECT_TSV_COLUMNS, _append_to_subject_tsv


def _sample_qs() -> list[tuple[int, str, int]]:
    return [
        (0, "I feel just the right amount of challenge.", 7),
        (1, "My thoughts run smoothly.", 7),
        (2, "I am tired.", 7),
    ]


def test_first_call_creates_file_with_header(tmp_path: Path) -> None:
    p = tmp_path / "sub-01_questionnaire.tsv"
    _append_to_subject_tsv(
        p,
        subject="01",
        session="001",
        task_name="task-mario_phase-discovery_run-01",
        run_idx=1,
        questions=_sample_qs(),
        responses=[3, 5, 2],
        submit_time=12345.6789,
    )
    # Force string parsing for label columns; pandas would otherwise
    # collapse "01" → int(1) and we'd lose the BIDS-compliant padding.
    df = pd.read_csv(p, sep="\t", dtype={"subject": str, "session": str, "task_name": str})
    assert list(df.columns) == _SUBJECT_TSV_COLUMNS
    assert len(df) == 3
    assert df["value"].tolist() == [3, 5, 2]
    assert df["question_idx"].tolist() == [0, 1, 2]
    assert (df["subject"] == "01").all()
    assert (df["session"] == "001").all()
    assert (df["task_name"] == "task-mario_phase-discovery_run-01").all()


def test_second_call_appends_without_re_writing_header(tmp_path: Path) -> None:
    p = tmp_path / "sub-01_questionnaire.tsv"
    _append_to_subject_tsv(
        p, subject="01", session="001",
        task_name="task-mario_phase-discovery_run-01", run_idx=1,
        questions=_sample_qs(), responses=[1, 2, 3], submit_time=100.0,
    )
    _append_to_subject_tsv(
        p, subject="01", session="001",
        task_name="task-mario_phase-discovery_run-02", run_idx=2,
        questions=_sample_qs(), responses=[4, 5, 6], submit_time=200.0,
    )
    df = pd.read_csv(p, sep="\t")
    assert len(df) == 6
    # First three rows are from run 1, next three from run 2.
    assert df["run_idx"].tolist() == [1, 1, 1, 2, 2, 2]
    assert df["value"].tolist() == [1, 2, 3, 4, 5, 6]
    # Header line is not duplicated.
    raw_lines = p.read_text().strip().split("\n")
    assert raw_lines[0].startswith("subject\t")
    assert sum(line.startswith("subject\t") for line in raw_lines) == 1


def test_creates_parent_dir(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "sub-01_questionnaire.tsv"
    _append_to_subject_tsv(
        p, subject="01", session="001",
        task_name="task-mario_run-01", run_idx=1,
        questions=_sample_qs(), responses=[1, 1, 1], submit_time=0.0,
    )
    assert p.is_file()
    assert p.parent.is_dir()


def test_question_text_is_preserved_verbatim(tmp_path: Path) -> None:
    p = tmp_path / "sub-01_questionnaire.tsv"
    _append_to_subject_tsv(
        p, subject="01", session="001",
        task_name="task-mario_run-01", run_idx=1,
        questions=questionnaire.build_default_questions(include_other=True),
        responses=[3] * 12,
        submit_time=0.0,
    )
    df = pd.read_csv(p, sep="\t")
    assert len(df) == 12
    # The first question's text matches the upstream FLOW_RATINGS list.
    assert df.iloc[0]["question_text"] == questionnaire.FLOW_RATINGS[0]
    # The last two come from OTHER_RATINGS.
    assert df.iloc[10]["question_text"] == questionnaire.OTHER_RATINGS[0]
    assert df.iloc[11]["question_text"] == questionnaire.OTHER_RATINGS[1]
