"""BIDS path resolution for mario_task outputs and data-root validation.

This module owns *all* file-system paths the experiment writes to or reads
from. By centralizing them in one dataclass we get:

* a single source of truth for BIDS conventions,
* an easy way for downstream code to swap in temporary paths during tests,
* a clean place to validate that the ROM and per-level save-state files
  are actually on disk before the experiment tries to start.

The layout (matches the upstream `task_stimuli` BIDS structure):

::

    output_root/
    └── sourcedata/
        └── sub-<subject>/
            ├── sub-<subject>_phase-discovery_task-mario_savestate.json
            ├── sub-<subject>_phase-stable_task-mario_savestate.json
            └── ses-<session>/
                ├── sub-<subject>_ses-<session>_<timestamp>.log
                ├── sub-<subject>_ses-<session>_<timestamp>_<taskname>_events.tsv
                └── sub-<subject>_ses-<session>_<timestamp>_<taskname>_<game>_<state>.bk2

The per-subject practice design TSV lives at the subject level too::

    output_root/sourcedata/sub-<subject>/sub-<subject>_design.tsv

so deleting the subject directory wipes every trace of that subject —
savestates, design, session outputs, and cumulative questionnaire
answers. No scattered state under ``data/``.

Pure stdlib; no psychopy / retro imports. Safe to use from tests.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


# BIDS allows alphanumerics only in subject and session labels. We accept
# the strict-but-friendly superset {alnum, dash, underscore} and reject
# everything else (most importantly path separators and shell metachars).
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _validate_label(name: str, label: str) -> None:
    if not _LABEL_RE.fullmatch(label):
        raise ValueError(
            f"Invalid {name} label {label!r}: must match {_LABEL_RE.pattern} "
            f"(alphanumeric, dash, underscore; cannot start with dash/underscore)."
        )


def make_timestamp() -> str:
    """Return a BIDS-friendly current timestamp string: ``YYYYMMDD-HHMMSS``."""
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def normalize_subject(raw: str) -> str:
    """Strip BIDS ``sub-`` prefix if the user typed one.

    >>> normalize_subject("sub-01")
    '01'
    >>> normalize_subject("01")
    '01'
    >>> normalize_subject("sub-pilot1")
    'pilot1'
    """
    return raw[4:] if raw.startswith("sub-") else raw


def infer_next_session(output_root: str | Path, subject: str) -> str:
    """Return the next zero-padded session number for ``subject``.

    Scans ``output_root/sourcedata/sub-<subject>/ses-*/`` for numeric
    session labels; returns ``max(existing) + 1`` formatted as a
    **3-digit** string (``"001"``, ``"002"``, ...). Returns ``"001"``
    if no sessions exist yet.

    Non-numeric session labels (e.g. ``ses-pilot``) are ignored when
    picking the next number.
    """
    import re

    subj_dir = Path(output_root) / "sourcedata" / f"sub-{subject}"
    if not subj_dir.is_dir():
        return "001"
    pat = re.compile(r"^ses-(\d+)$")
    nums: list[int] = []
    for child in subj_dir.iterdir():
        if not child.is_dir():
            continue
        m = pat.match(child.name)
        if m:
            nums.append(int(m.group(1)))
    return f"{(max(nums) + 1) if nums else 1:03d}"


@dataclass(frozen=True)
class BidsPaths:
    """Resolved file-system paths for a single subject / session / run.

    All paths are :class:`pathlib.Path` instances. None of them are created
    automatically; the experiment runner is responsible for ``mkdir``ing
    the directories it needs (so test code can also point the dataclass at
    a tmp dir without side-effects).

    Attributes:
        subject:     Subject label, e.g. ``"01"``. No ``sub-`` prefix.
        session:     Session label, e.g. ``"001"`` (3-digit by convention).
                     No ``ses-`` prefix.
        output_root: Root directory for the BIDS tree (typically ``./output``).
        timestamp:   Run timestamp, ``YYYYMMDD-HHMMSS``. Defaults to ``make_timestamp()``.
    """

    subject: str
    session: str
    output_root: Path
    timestamp: str = field(default_factory=make_timestamp)

    def __post_init__(self) -> None:
        _validate_label("subject", self.subject)
        _validate_label("session", self.session)
        # Normalize the output root to a Path. Dataclass(frozen=True) +
        # object.__setattr__ lets us coerce without breaking immutability.
        if not isinstance(self.output_root, Path):
            object.__setattr__(self, "output_root", Path(self.output_root))

    # ----- directories -----

    @property
    def sourcedata_subject_dir(self) -> Path:
        return self.output_root / "sourcedata" / f"sub-{self.subject}"

    @property
    def sourcedata_session_dir(self) -> Path:
        return self.sourcedata_subject_dir / f"ses-{self.session}"

    # ----- per-file paths -----

    @property
    def log_path(self) -> Path:
        return self.sourcedata_session_dir / f"{self.session_prefix}.log"

    @property
    def session_prefix(self) -> str:
        """Filename prefix shared by every per-task artifact in this session."""
        return f"sub-{self.subject}_ses-{self.session}_{self.timestamp}"

    def events_tsv(self, task_name: str) -> Path:
        """Path for a task's BIDS events TSV.

        Args:
            task_name: e.g. ``"task-mario_phase-discovery_run-01"`` or
                       ``"task-mario_run-03"``. Must not contain path separators.
        """
        _validate_task_name(task_name)
        return self.sourcedata_session_dir / f"{self.session_prefix}_{task_name}_events.tsv"

    def movie_path(self, task_name: str, state_name: str, rep_idx: int) -> Path:
        """Path for a single bk2 emulator recording.

        Naming: ``<prefix>_<task_name>_<state_name>_rep-<NN>.bk2`` where
        ``rep_idx`` is the *within-run* attempt counter (1-indexed). The
        emulator game name is intentionally NOT in the filename — the
        repo only handles SuperMarioBros-Nes, so it would be redundant
        noise.

        Args:
            task_name:  BIDS task name, e.g. ``"task-mario_phase-discovery_run-01"``.
            state_name: Level name as exposed by stable-retro, e.g. ``"Level1-1"``.
            rep_idx:    1-indexed attempt counter, scoped to the run.
        """
        _validate_task_name(task_name)
        return (
            self.sourcedata_session_dir
            / f"{self.session_prefix}_{task_name}_{state_name}_rep-{rep_idx:02d}.bk2"
        )

    def savestate(self, phase: Literal["discovery", "stable"]) -> Path:
        """Path for a phase savestate JSON (cross-session, per-subject)."""
        if phase not in ("discovery", "stable"):
            raise ValueError(f"phase must be 'discovery' or 'stable', got {phase!r}")
        return (
            self.sourcedata_subject_dir
            / f"sub-{self.subject}_phase-{phase}_task-mario_savestate.json"
        )

    @property
    def design_tsv(self) -> Path:
        """Path to this subject's practice-phase design TSV.

        Lives under the per-subject sourcedata directory so deleting the
        subject dir wipes every trace of the subject (savestates, design,
        cumulative questionnaire, session outputs).
        """
        return self.sourcedata_subject_dir / f"sub-{self.subject}_design.tsv"

    @property
    def questionnaire_tsv(self) -> Path:
        """Path to this subject's cumulative questionnaire answers TSV.

        One row per question per submission, accumulated across every run
        and session for this subject. Convenient for downstream analysis
        without having to walk every per-task events.tsv.
        """
        return self.sourcedata_subject_dir / f"sub-{self.subject}_questionnaire.tsv"


def _validate_task_name(name: str) -> None:
    # task_name flows into a filename; reject anything that would let it
    # escape the session directory.
    if "/" in name or "\\" in name or ".." in name or name.startswith("."):
        raise ValueError(f"invalid task_name {name!r}")


# ---------------------------------------------------------------------------
# Data-root validation (ROM + level save-states)
# ---------------------------------------------------------------------------

_REQUIRED_LEVELS: list[str] = [
    f"Level{world}-{level}.state"
    # Local import to avoid creating a runtime cycle: design.py imports nothing
    # from paths.py, but paths.py wants design.ALL_LEVELS to stay in sync
    # with whatever discovery/practice considers "the 22 levels".
    for world, level in __import__("mario_task.design", fromlist=["ALL_LEVELS"]).ALL_LEVELS
]


def check_data_root(data_root: str | Path) -> str | None:
    """Return ``None`` if the ROM/state data is ready, else an actionable error.

    The error string is meant to be printed to the operator; it tells them
    *exactly* what's missing and *how* to recover (typically: run datalad
    get inside ``data/mario.stimuli/``).

    Required layout under ``data_root`` (the ``SuperMarioBros-Nes`` dir
    inside a ``mario.stimuli`` checkout)::

        data_root/
          rom.nes
          data.json
          scenario.json
          Level1-1.state
          Level1-2.state
          ...
          Level8-3.state
    """
    root = Path(data_root)
    if not root.is_dir():
        return (
            f"Mario data directory not found: {root!s}\n"
            f"Run `bash setup_env.sh` to fetch it via datalad."
        )

    missing: list[str] = []

    rom = root / "rom.nes"
    if not rom.exists():
        missing.append("rom.nes")
    elif not rom.is_file() or rom.stat().st_size == 0:
        # A dangling git-annex symlink resolves to ``not is_file()``;
        # a not-yet-fetched annex pointer can be a symlink that resolves
        # to a 0-byte real file. Either way it's broken.
        return (
            f"rom.nes at {rom!s} is empty or a dangling symlink.\n"
            f"This usually means `datalad get` hasn't fetched the file yet. "
            f"From the repo root: `cd data/mario.stimuli && datalad get .`"
        )

    for needed in ("data.json", "scenario.json"):
        if not (root / needed).is_file():
            missing.append(needed)

    for state_name in _REQUIRED_LEVELS:
        p = root / state_name
        if not p.is_file() or p.stat().st_size == 0:
            missing.append(state_name)

    if missing:
        listing = "\n  ".join(missing[:10])
        more = f"\n  ... and {len(missing) - 10} more" if len(missing) > 10 else ""
        return (
            f"Missing or empty files in {root!s}:\n  {listing}{more}\n"
            f"From the repo root: `cd data/mario.stimuli && datalad get .`"
        )

    return None
