"""Deterministic per-subject design TSV generation.

The practice phase plays a long pre-shuffled sequence of levels stored in
``data/videogames/mario/designs/sub-<subject>_design.tsv``. Each "repetition"
is a fresh random permutation of the 22 unique levels (8 worlds * 3 levels
minus the two excluded pairs (2,2) and (7,2); the X-4 castle levels are
never visited because the inner loop range stops at 3). With ``N_REPS = 50``
the TSV has 1100 rows.

The random seed is derived from ``sha1(subject_id)`` so that the same
subject ID always produces the same TSV — across machines, Python
versions, and runs.

The module is pure: stdlib + pandas only. It must remain importable
without psychopy or stable-retro.
"""

from __future__ import annotations

import hashlib
import os
import random
from pathlib import Path

import pandas as pd

WORLDS = 8
LEVELS_PER_WORLD = 3  # only "outdoor" levels: skips all X-4 castle levels
EXCLUDE = frozenset({(2, 2), (7, 2)})
N_REPS = 50

ALL_LEVELS: list[tuple[int, int]] = [
    (world, level)
    for world in range(1, WORLDS + 1)
    for level in range(1, LEVELS_PER_WORLD + 1)
    if (world, level) not in EXCLUDE
]
"""The 22 unique levels visited across discovery + practice."""

N_LEVELS_PER_RUN = len(ALL_LEVELS)
"""How many design-TSV rows a single practice run consumes (== one full pass through all 22)."""


def _seed_for_subject(subject: str) -> int:
    """Deterministic 32-bit seed from sha1(subject_id).

    sha1 → 160-bit hex → int → mod 2**32 - 1.
    """
    digest = hashlib.sha1(subject.encode("utf-8")).hexdigest()
    return int(digest, 16) % (2**32 - 1)


def generate_design(subject: str, n_reps: int = N_REPS) -> pd.DataFrame:
    """Return the level sequence for ``subject`` as a DataFrame.

    Columns: ``world`` (int), ``level`` (int). Length: ``n_reps * 22``.
    Each contiguous block of 22 rows is one full random permutation of
    :data:`ALL_LEVELS`.
    """
    seed = _seed_for_subject(subject)
    rng = random.Random(seed)
    rows: list[tuple[int, int]] = []
    for _ in range(n_reps):
        # random.sample with k == len returns a shuffled copy. We use it
        # (rather than rng.shuffle on a mutable list) so each repetition
        # is built from a fresh permutation drawn from ALL_LEVELS.
        rows.extend(rng.sample(ALL_LEVELS, k=len(ALL_LEVELS)))
    return pd.DataFrame(rows, columns=("world", "level"))


def design_path(subject: str, designs_dir: str | os.PathLike[str]) -> Path:
    """Return the canonical path to ``subject``'s design TSV."""
    return Path(designs_dir) / f"sub-{subject}_design.tsv"


def ensure_design(
    subject: str,
    designs_dir: str | os.PathLike[str],
    n_reps: int = N_REPS,
    overwrite: bool = False,
) -> Path:
    """Generate the design TSV if it doesn't exist (or if ``overwrite=True``).

    Returns the path to the (existing or freshly written) file.
    """
    path = design_path(subject, designs_dir)
    if path.exists() and not overwrite:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    df = generate_design(subject, n_reps=n_reps)
    # Atomic write: write to .tmp then rename. Same contract as savestate.save.
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, sep="\t", index=False)
    os.replace(tmp, path)
    return path
