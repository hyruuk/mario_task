"""Deterministic per-subject practice-phase design TSV.

The practice phase plays a long pre-shuffled sequence of levels stored
at ``output/sourcedata/sub-<subject>/sub-<subject>_design.tsv``. Each
"epoch" is a fresh random permutation of the *enabled* levels — by
default the original 22 (8 worlds × 3 outdoor levels, minus the two
excluded pairs (2,2) and (7,2); the X-4 castle levels are skipped). The
user can enable any of the 32 possible levels in ``config.json``.

With ``N_REPS = 50`` the TSV has ``50 × len(enabled_levels)`` rows, more
than enough to outlast any practice session.

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
from typing import Iterable

import pandas as pd

WORLDS = 8

# Every level present in NES Super Mario Bros: 8 worlds × 4 levels each
# (X-1 outdoor → X-2 → X-3 → X-4 castle). Used by the wizard / config
# layer to decide which levels can be enabled.
ALL_POSSIBLE_LEVELS: tuple[tuple[int, int], ...] = tuple(
    (world, level)
    for world in range(1, WORLDS + 1)
    for level in range(1, 5)
)
"""All 32 NES SMB levels (X-1..X-4 across 8 worlds)."""

# Levels excluded from the canonical 22-level set. (2,2) and (7,2) are
# excluded for historical reasons (broken / unfair in retro); X-4
# castles are excluded because they're shorter and play very differently
# from the outdoor levels.
_DEFAULT_EXCLUDED: frozenset[tuple[int, int]] = frozenset(
    {(2, 2), (7, 2)} | {(w, 4) for w in range(1, WORLDS + 1)}
)

# The "canonical" 22-level set: the default value of
# ``Settings.task.enabled_levels``. Users can override to enable any
# subset of ALL_POSSIBLE_LEVELS (including the castles).
DEFAULT_ENABLED_LEVELS: tuple[tuple[int, int], ...] = tuple(
    lvl for lvl in ALL_POSSIBLE_LEVELS if lvl not in _DEFAULT_EXCLUDED
)
"""The 22 unique levels enabled by default for discovery + practice."""

N_REPS = 50

# Legacy aliases. Kept so older code (and tests) that import these
# constants keep working; both reference the default enabled set.
ALL_LEVELS: tuple[tuple[int, int], ...] = DEFAULT_ENABLED_LEVELS
N_LEVELS_PER_RUN = len(DEFAULT_ENABLED_LEVELS)


def _seed_for_subject(subject: str) -> int:
    """Deterministic 32-bit seed from sha1(subject_id).

    sha1 → 160-bit hex → int → mod 2**32 - 1.
    """
    digest = hashlib.sha1(subject.encode("utf-8")).hexdigest()
    return int(digest, 16) % (2**32 - 1)


def generate_design(
    subject: str,
    n_reps: int = N_REPS,
    enabled_levels: Iterable[tuple[int, int]] = DEFAULT_ENABLED_LEVELS,
) -> pd.DataFrame:
    """Return the level sequence for ``subject`` as a DataFrame.

    Columns: ``world`` (int), ``level`` (int). Length:
    ``n_reps * len(enabled_levels)``. Each contiguous block of
    ``len(enabled_levels)`` rows is one full random permutation of
    ``enabled_levels`` — the depleting-pool semantics within an epoch.

    The seed combines ``sha1(subject)`` with the enabled-levels tuple so
    changing the level set produces a different (but still
    deterministic) shuffle for the same subject.
    """
    enabled = tuple(enabled_levels)
    if not enabled:
        raise ValueError("enabled_levels must be non-empty")
    seed = _seed_for_subject(subject)
    # XOR in a hash of the enabled levels so the shuffle differs when
    # the set changes; pure-Python ``hash()`` is randomized between
    # processes so use sha1 again for determinism.
    h = hashlib.sha1(repr(enabled).encode("utf-8")).hexdigest()
    seed ^= int(h, 16) % (2**32 - 1)
    rng = random.Random(seed)
    rows: list[tuple[int, int]] = []
    for _ in range(n_reps):
        rows.extend(rng.sample(enabled, k=len(enabled)))
    return pd.DataFrame(rows, columns=("world", "level"))


def ensure_design(
    path: str | os.PathLike[str],
    subject: str,
    *,
    n_reps: int = N_REPS,
    enabled_levels: Iterable[tuple[int, int]] = DEFAULT_ENABLED_LEVELS,
    overwrite: bool = False,
) -> Path:
    """Generate the design TSV at ``path`` if missing (or ``overwrite=True``).

    Atomic: writes to ``<path>.tmp`` then ``os.replace``, so a Ctrl+C
    mid-write can't leave a corrupted TSV behind.

    Returns the path (whether it already existed or was just written).

    Note: if the user changes ``enabled_levels`` after the TSV is
    written, the on-disk TSV won't be auto-regenerated — pass
    ``overwrite=True`` or delete the file manually to refresh.
    """
    p = Path(path)
    if p.exists() and not overwrite:
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    df = generate_design(subject, n_reps=n_reps, enabled_levels=enabled_levels)
    tmp = p.with_suffix(p.suffix + ".tmp")
    df.to_csv(tmp, sep="\t", index=False)
    os.replace(tmp, p)
    return p
