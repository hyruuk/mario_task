"""Tests for the phase generator.

We stub the Task and Prompt factories with simple dataclasses so the
generator can be driven without psychopy or retro.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd
import pytest

from mario_task import savestate
from mario_task.design import ALL_LEVELS, N_LEVELS_PER_RUN, generate_design
from mario_task.paths import BidsPaths
from mario_task.phases import (
    advance_discovery_state,
    current_level_name,
    discovery_complete,
    iter_tasks,
    practice_levels_from,
)
from mario_task.settings import TaskSettings


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_advance_discovery_state_within_world() -> None:
    s = advance_discovery_state({"world": 1, "level": 1})
    assert s == {"world": 1, "level": 2}


def test_advance_discovery_state_wraps_to_next_world() -> None:
    s = advance_discovery_state({"world": 1, "level": 3})
    assert s == {"world": 2, "level": 1}


def test_advance_discovery_state_skips_2_2() -> None:
    s = advance_discovery_state({"world": 2, "level": 1})
    # Next should be 2-3 (skipping 2-2).
    assert s == {"world": 2, "level": 3}


def test_advance_discovery_state_skips_7_2() -> None:
    s = advance_discovery_state({"world": 7, "level": 1})
    assert s == {"world": 7, "level": 3}


def test_advance_discovery_state_terminates_at_world_9() -> None:
    s = advance_discovery_state({"world": 8, "level": 3})
    assert discovery_complete(s)
    assert s["world"] == 9


def test_full_discovery_walk_covers_exactly_all_levels() -> None:
    visited: list[tuple[int, int]] = []
    state = {"world": 1, "level": 1}
    for _ in range(100):  # generous upper bound
        if discovery_complete(state):
            break
        visited.append((state["world"], state["level"]))
        state = advance_discovery_state(state)
    assert sorted(visited) == sorted(ALL_LEVELS)
    assert len(visited) == 22


def test_current_level_name_formats_correctly() -> None:
    assert current_level_name({"world": 1, "level": 1}) == "Level1-1"
    assert current_level_name({"world": 8, "level": 3}) == "Level8-3"


def test_practice_levels_from_returns_all_remaining() -> None:
    design = generate_design("sub01", n_reps=3)
    levels = practice_levels_from(design, 0)
    assert len(levels) == 3 * N_LEVELS_PER_RUN
    assert all(name.startswith("Level") for name in levels)


def test_practice_levels_from_returns_empty_when_exhausted() -> None:
    design = generate_design("sub01", n_reps=1)
    assert practice_levels_from(design, 99999) == []


def test_practice_levels_from_starts_at_offset() -> None:
    design = generate_design("sub01", n_reps=2)
    levels = practice_levels_from(design, 22)
    # 2 reps × 22 = 44 rows total; starting at index 22 gives the
    # 22-entry tail (second shuffle).
    assert len(levels) == N_LEVELS_PER_RUN


# ---------------------------------------------------------------------------
# iter_tasks fixtures and stubs
# ---------------------------------------------------------------------------


@dataclass
class StubTask:
    """Stub task object. Tests set the completion flags before yielding."""

    levels: list[str]
    run_idx: int
    _completed: bool = False
    _task_completed: bool = False
    _nlevels: int = 0


@dataclass
class StubPrompt:
    pressed: str = "continue"  # "continue" | "end"


@pytest.fixture
def bids_paths(tmp_path: Path) -> BidsPaths:
    bp = BidsPaths(
        subject="sub01",
        session="001",
        output_root=tmp_path / "output",
        timestamp="20260526-150000",
    )
    bp.sourcedata_subject_dir.mkdir(parents=True)
    bp.sourcedata_session_dir.mkdir(parents=True)
    # Pre-generate the per-subject design TSV so practice has something to read.
    generate_design("sub01", n_reps=2).to_csv(bp.design_tsv, sep="\t", index=False)
    return bp


def _drive(
    paths: BidsPaths,
    settings: TaskSettings,
    *,
    discovery_outcomes: list[bool] | None = None,
    practice_outcomes: list[tuple[bool, int]] | None = None,  # (completed, nlevels)
    prompt_decisions: list[str] | None = None,
) -> tuple[list, list]:
    """Drive ``iter_tasks`` with scripted outcomes; return what was yielded."""
    discovery_outcomes = list(discovery_outcomes or [])
    practice_outcomes = list(practice_outcomes or [])
    prompt_decisions = list(prompt_decisions or [])

    tasks_seen: list[StubTask] = []
    prompts_seen: list[StubPrompt] = []

    def make_discovery_task(level_name: str, run_idx: int) -> StubTask:
        t = StubTask(levels=[level_name], run_idx=run_idx)
        # The yield happens between task creation and outcome injection;
        # we set the result lazily right before iter_tasks resumes.
        if discovery_outcomes:
            outcome = discovery_outcomes.pop(0)
            t._completed = outcome
        tasks_seen.append(t)
        return t

    def make_practice_task(levels: list[str], run_idx: int) -> StubTask:
        t = StubTask(levels=list(levels), run_idx=run_idx)
        if practice_outcomes:
            done, nlevels = practice_outcomes.pop(0)
            t._task_completed = done
            t._nlevels = nlevels
        tasks_seen.append(t)
        return t

    def make_prompt() -> StubPrompt:
        decision = prompt_decisions.pop(0) if prompt_decisions else "end"
        p = StubPrompt(pressed=decision)
        prompts_seen.append(p)
        return p

    yielded: list = []
    gen = iter_tasks(
        paths,
        settings,
        make_discovery_task=make_discovery_task,
        make_practice_task=make_practice_task,
        make_prompt=make_prompt,
    )
    for item in gen:
        yielded.append(item)
    return yielded, prompts_seen


# ---------------------------------------------------------------------------
# iter_tasks: discovery flow
# ---------------------------------------------------------------------------


def test_iter_tasks_starts_in_discovery_for_fresh_subject(bids_paths: BidsPaths) -> None:
    settings = TaskSettings()
    yielded, _ = _drive(
        bids_paths,
        settings,
        discovery_outcomes=[True],
        prompt_decisions=["end"],
    )
    # 1 task + 1 prompt
    assert len(yielded) == 2
    assert isinstance(yielded[0], StubTask)
    assert yielded[0].levels == ["Level1-1"]


def test_iter_tasks_advances_discovery_savestate_on_completion(bids_paths: BidsPaths) -> None:
    settings = TaskSettings()
    _drive(
        bids_paths,
        settings,
        discovery_outcomes=[True],
        prompt_decisions=["end"],
    )
    state = savestate.load(bids_paths.savestate("discovery"))
    assert state == {"world": 1, "level": 2}


def test_iter_tasks_does_not_advance_when_run_uncompleted(bids_paths: BidsPaths) -> None:
    settings = TaskSettings()
    _drive(
        bids_paths,
        settings,
        discovery_outcomes=[False],
        prompt_decisions=["end"],
    )
    assert not bids_paths.savestate("discovery").exists()


def test_iter_tasks_continue_press_yields_another_task(bids_paths: BidsPaths) -> None:
    settings = TaskSettings()
    yielded, _ = _drive(
        bids_paths,
        settings,
        discovery_outcomes=[True, True],
        prompt_decisions=["continue", "end"],
    )
    tasks = [y for y in yielded if isinstance(y, StubTask)]
    assert len(tasks) == 2
    assert tasks[0].levels == ["Level1-1"]
    assert tasks[1].levels == ["Level1-2"]


def test_iter_tasks_resumes_from_existing_discovery_savestate(bids_paths: BidsPaths) -> None:
    savestate.save(bids_paths.savestate("discovery"), {"world": 3, "level": 1})
    yielded, _ = _drive(
        bids_paths,
        TaskSettings(),
        discovery_outcomes=[True],
        prompt_decisions=["end"],
    )
    assert yielded[0].levels == ["Level3-1"]  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# iter_tasks: phase transition
# ---------------------------------------------------------------------------


def test_iter_tasks_falls_through_into_practice_when_discovery_completes(
    bids_paths: BidsPaths,
) -> None:
    # Subject is one level away from finishing discovery.
    savestate.save(bids_paths.savestate("discovery"), {"world": 8, "level": 3})
    yielded, _ = _drive(
        bids_paths,
        TaskSettings(),
        discovery_outcomes=[True],  # beats Level8-3
        practice_outcomes=[(True, N_LEVELS_PER_RUN)],
        prompt_decisions=["continue", "end"],
    )
    tasks = [y for y in yielded if isinstance(y, StubTask)]
    assert len(tasks) == 2
    # Run 1 = discovery (single level); Run 2 = practice (gets all 44
    # remaining design entries since fixture has n_reps=2).
    assert len(tasks[0].levels) == 1
    assert len(tasks[1].levels) == 2 * N_LEVELS_PER_RUN
    # Stable savestate was created during the transition; index advances
    # by the actual number of levels played in the practice run
    # (N_LEVELS_PER_RUN per the stub's _nlevels), not by the slice length.
    assert bids_paths.savestate("stable").exists()
    assert savestate.load(bids_paths.savestate("stable"))["index"] == N_LEVELS_PER_RUN


def test_iter_tasks_skips_discovery_when_stable_savestate_exists(
    bids_paths: BidsPaths,
) -> None:
    savestate.save(bids_paths.savestate("stable"), {"index": 0})
    yielded, _ = _drive(
        bids_paths,
        TaskSettings(),
        practice_outcomes=[(True, N_LEVELS_PER_RUN)],
        prompt_decisions=["end"],
    )
    tasks = [y for y in yielded if isinstance(y, StubTask)]
    assert len(tasks) == 1
    # Practice gets ALL remaining design entries (2 * N = 44 in the fixture).
    assert len(tasks[0].levels) == 2 * N_LEVELS_PER_RUN


# ---------------------------------------------------------------------------
# iter_tasks: end-of-run prompt
# ---------------------------------------------------------------------------


def test_iter_tasks_end_press_stops_iterator(bids_paths: BidsPaths) -> None:
    yielded, _ = _drive(
        bids_paths,
        TaskSettings(),
        discovery_outcomes=[True],
        prompt_decisions=["end"],
    )
    # Exactly 2 items: 1 task, 1 prompt. The iterator stops.
    assert len(yielded) == 2


def test_iter_tasks_continue_then_end(bids_paths: BidsPaths) -> None:
    yielded, _ = _drive(
        bids_paths,
        TaskSettings(),
        discovery_outcomes=[True, False, True],  # variable outcomes
        prompt_decisions=["continue", "continue", "end"],
    )
    # 3 tasks + 3 prompts.
    assert sum(isinstance(y, StubTask) for y in yielded) == 3
    assert sum(isinstance(y, StubPrompt) for y in yielded) == 3


# ---------------------------------------------------------------------------
# iter_tasks: settings flags
# ---------------------------------------------------------------------------


def test_discovery_disabled_jumps_straight_to_practice(bids_paths: BidsPaths) -> None:
    settings = TaskSettings(discovery_enabled=False, practice_enabled=True)
    yielded, _ = _drive(
        bids_paths,
        settings,
        practice_outcomes=[(True, N_LEVELS_PER_RUN)],
        prompt_decisions=["end"],
    )
    tasks = [y for y in yielded if isinstance(y, StubTask)]
    assert len(tasks) == 1
    # Practice gets the entire remaining design (2 reps × 22 = 44 levels).
    assert len(tasks[0].levels) == 2 * N_LEVELS_PER_RUN
    # Stable savestate created on bootstrap.
    assert bids_paths.savestate("stable").exists()


def test_practice_disabled_after_discovery_completes_ends_session(
    bids_paths: BidsPaths,
) -> None:
    savestate.save(bids_paths.savestate("discovery"), {"world": 8, "level": 3})
    settings = TaskSettings(discovery_enabled=True, practice_enabled=False)
    yielded, _ = _drive(
        bids_paths,
        settings,
        discovery_outcomes=[True],
        prompt_decisions=["continue"],  # operator wants to keep going but practice is off
    )
    # Just the final discovery run + its prompt; then the generator ends
    # because practice is disabled and the stable savestate now exists.
    tasks = [y for y in yielded if isinstance(y, StubTask)]
    assert len(tasks) == 1


def test_practice_ends_when_design_exhausted(bids_paths: BidsPaths) -> None:
    # 1-rep design = 22 rows. Start index at row 22 → no levels left.
    savestate.save(bids_paths.savestate("stable"), {"index": 999})
    yielded, _ = _drive(bids_paths, TaskSettings(), prompt_decisions=["end"])
    assert yielded == []
