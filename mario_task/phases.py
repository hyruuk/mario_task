"""Discovery and practice phase orchestration.

A *session* is a sequence of *runs*. Between every run the operator
chooses (via :class:`EndOfRunPrompt`-style UI) whether to continue with
another run or end the session. Sessions are therefore variable-length.

There are two phases, in this order:

**Discovery** — one level per run. The same level is replayed for the
whole run (the underlying engine cycles ``state_names=[level]`` with
``repeat_scenario=True`` until ``max_duration``). Advancement to the
next level happens at the *run boundary*, only if the run was completed
(``task._completed is True``). Progression order:
``Level1-1 → 1-2 → 1-3 → 2-1 → 2-3 → 3-1 → ... → 8-3`` — skips ``(2,2)``,
``(7,2)`` and all X-4 castle levels. Discovery is done once the
internal world counter reaches 9.

**Practice** — each run plays a contiguous slice of the per-subject
design TSV (22 consecutive entries = one full random pass through all
22 levels). The slice index advances only if the run completed
(``task._task_completed is True``).

When discovery completes (mid-session or at session start), the
``_phase-stable_*_savestate.json`` file is created and we fall through
into practice in the same session. Conversely, an existing stable
savestate means the subject has already moved on, so discovery is
skipped entirely.

This module is pure-Python: no psychopy, no retro. The actual task and
prompt objects are injected as factory callables, so the orchestration
logic can be unit-tested without a display.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterator, Protocol

import pandas as pd

from mario_task import savestate
from mario_task.design import DEFAULT_ENABLED_LEVELS
from mario_task.paths import BidsPaths
from mario_task.settings import TaskSettings

# Sentinel used in the discovery savestate when all enabled levels have
# been cleared. ``world == _DISCOVERY_DONE_WORLD`` means "past the last
# enabled level" — phases.iter_tasks treats this as the trigger to
# write the stable savestate and transition into practice.
_DISCOVERY_DONE_WORLD = 9


# ---------------------------------------------------------------------------
# Task / prompt protocols (avoids importing real classes in Phase 0)
# ---------------------------------------------------------------------------


class HasCompletedFlag(Protocol):
    """A Task whose ``_completed`` / ``_task_completed`` flags drive savestate advancement."""

    _completed: bool
    _task_completed: bool
    _nlevels: int


class HasPressedFlag(Protocol):
    """A prompt whose ``pressed`` attribute tells us whether to continue or end."""

    pressed: str  # "continue" | "end"


MakeDiscoveryTask = Callable[[str, int], HasCompletedFlag]
"""Factory: ``(level_name, run_idx) -> Task``."""

MakePracticeTask = Callable[[list[str], int], HasCompletedFlag]
"""Factory: ``(level_names, run_idx) -> Task``."""

MakePrompt = Callable[[], HasPressedFlag]
"""Factory: ``() -> Prompt``."""


# ---------------------------------------------------------------------------
# Pure discovery-state machinery
# ---------------------------------------------------------------------------


def advance_discovery_state(
    state: dict[str, int],
    enabled_levels: tuple[tuple[int, int], ...] = DEFAULT_ENABLED_LEVELS,
) -> dict[str, int]:
    """Return the next ``{world, level}`` after a successful run.

    Walks through ``enabled_levels`` in the order the operator
    specified. Returns ``{world: 9, level: 1}`` once the last enabled
    level has been cleared (the sentinel :func:`discovery_complete`
    checks for).

    Edge cases:
        * If the current ``(world, level)`` is not in ``enabled_levels``
          (operator changed the level set mid-progression), the function
          treats the next state as ``enabled_levels[0]`` so progress
          resumes from the first enabled level.
        * The function is pure — it never reads or writes files.
    """
    current = (int(state["world"]), int(state["level"]))
    try:
        idx = enabled_levels.index(current)
    except ValueError:
        # current level no longer enabled; restart from the first.
        return {"world": enabled_levels[0][0], "level": enabled_levels[0][1]}
    next_idx = idx + 1
    if next_idx >= len(enabled_levels):
        return {"world": _DISCOVERY_DONE_WORLD, "level": 1}
    w, l = enabled_levels[next_idx]
    return {"world": w, "level": l}


def discovery_complete(state: dict[str, int]) -> bool:
    return int(state.get("world", 1)) >= _DISCOVERY_DONE_WORLD


def current_level_name(state: dict[str, int]) -> str:
    return f"Level{state['world']}-{state['level']}"


# ---------------------------------------------------------------------------
# Practice-state machinery
# ---------------------------------------------------------------------------


def _read_design(design_tsv_path: Path) -> pd.DataFrame:
    return pd.read_csv(design_tsv_path, sep="\t")


def practice_levels_from(design: pd.DataFrame, index: int) -> list[str]:
    """Return every remaining level from row ``index`` onward.

    Practice runs play through these in order until ``max_duration``
    expires (the MarioTask checks the time after every attempt); the
    savestate index advances by the actual number played, not by the
    list length. Returns an empty list if the design is exhausted —
    phases.iter_tasks uses that as the "no more practice" signal.
    """
    slice_ = design.iloc[index:]
    return [f"Level{int(row['world'])}-{int(row['level'])}" for _, row in slice_.iterrows()]


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------


def iter_tasks(
    paths: BidsPaths,
    settings: TaskSettings,
    *,
    make_discovery_task: MakeDiscoveryTask,
    make_practice_task: MakePracticeTask,
    make_prompt: MakePrompt,
) -> Iterator[HasCompletedFlag | HasPressedFlag]:
    """Yield Task / Prompt instances for the current session.

    Behaviour:

    * If a discovery savestate exists, resume from it; otherwise start at
      world=1, level=1. Each run plays the current level; on
      ``task._completed`` we advance the savestate. When the world
      counter reaches :data:`_DISCOVERY_DONE_WORLD` we *create* the
      stable savestate (``{"index": 0}``) and fall through into practice
      within the same session.
    * If a stable savestate already existed at entry — discovery is
      skipped entirely.
    * Settings flags ``discovery_enabled`` / ``practice_enabled`` let
      the operator short-circuit either phase.
    * After every run we yield a prompt; if the operator picks ``"end"``
      we stop the iterator (the session ends cleanly).
    * Practice ends when the design TSV is exhausted.

    The factories take responsibility for actually constructing the
    Task / Prompt objects (which depend on psychopy + retro). This
    module is pure-Python and unit-testable without a display.
    """
    discovery_savestate_path = paths.savestate("discovery")
    stable_savestate_path = paths.savestate("stable")

    in_practice = stable_savestate_path.exists()

    # Defensive: if discovery is disabled and no stable savestate exists,
    # bootstrap the stable savestate so we go straight to practice.
    if not in_practice and not settings.discovery_enabled:
        if settings.practice_enabled:
            savestate.save(stable_savestate_path, {"index": 0})
            in_practice = True
        else:
            return

    run_idx = 0
    design_df: pd.DataFrame | None = None

    enabled_levels = tuple(settings.enabled_levels)
    initial_discovery_state = {
        "world": enabled_levels[0][0],
        "level": enabled_levels[0][1],
    }
    while True:
        if in_practice:
            if not settings.practice_enabled:
                return
            if design_df is None:
                design_df = _read_design(paths.design_tsv)
            state = savestate.load_or_default(stable_savestate_path, {"index": 0})
            levels = practice_levels_from(design_df, int(state["index"]))
            if not levels:
                return  # design exhausted
            task = make_practice_task(levels, run_idx)
            yield task
            # Only advance if the run wasn't interrupted (Ctrl+C). The
            # index moves by the actual number of levels played
            # (task._nlevels), not by a fixed slice size — practice runs
            # are variable-length, capped by max_duration.
            if getattr(task, "_task_completed", False):
                state = {"index": int(state["index"]) + int(getattr(task, "_nlevels", 0))}
                savestate.save(stable_savestate_path, state)
        else:
            state = savestate.load_or_default(
                discovery_savestate_path, initial_discovery_state
            )
            if discovery_complete(state):
                # Shouldn't normally happen (would've been caught at entry)
                # but if a stale discovery savestate has world>=9 and no
                # stable savestate exists, create one and switch.
                savestate.save(stable_savestate_path, {"index": 0})
                in_practice = True
                continue
            level_name = current_level_name(state)
            task = make_discovery_task(level_name, run_idx)
            yield task
            if getattr(task, "_completed", False):
                new_state = advance_discovery_state(state, enabled_levels)
                savestate.save(discovery_savestate_path, new_state)
                if discovery_complete(new_state):
                    # Transition into practice from the next run onward.
                    savestate.save(stable_savestate_path, {"index": 0})
                    in_practice = True

        # End-of-run prompt.
        prompt = make_prompt()
        yield prompt
        if getattr(prompt, "pressed", "continue") == "end":
            return
        run_idx += 1
