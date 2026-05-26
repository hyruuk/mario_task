"""Two operator-facing dialogs: first-run config wizard + per-session subject picker.

The dialogs are intentionally simple — both fit on a single
``psychopy.gui.Dlg`` screen so a non-technical operator can fill them
in without instructions. Compromises this implies:

* No reactive fields (psychopy.gui has no callbacks). The trigger
  ``port`` field is always shown; the wizard's documentation tells the
  operator that it's ignored when the backend is ``lsl`` or ``null``.
* The subject-picker shows the existing subjects' progress as a static
  read-only block above the input fields, computed once when the
  dialog opens. The operator picks one by typing the ID.

Pure helpers in this module (``list_existing_subjects``,
``format_subject_progress``, ``infer_default_session``) are CI-testable
without psychopy/display. The actual Dlg rendering needs a display so
is exercised by the integration smoke test instead.

The wizard / picker functions return ``None`` if the operator hits
Cancel — :mod:`mario_task.cli` interprets that as "abort cleanly,
exit code 0".
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from mario_task import savestate
from mario_task.design import N_LEVELS_PER_RUN
from mario_task.design import ALL_POSSIBLE_LEVELS, DEFAULT_ENABLED_LEVELS, WORLDS
from mario_task.settings import (
    DisplaySettings,
    PathSettings,
    Settings,
    TaskSettings,
    TriggerSettings,
    _VALID_BACKENDS,
    default_settings,
    save,
    supports_parallel_port,
)

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Pure helpers (CI-testable; no psychopy import here)
# ---------------------------------------------------------------------------


_SUB_DIR_RX = re.compile(r"^sub-(?P<label>[A-Za-z0-9][A-Za-z0-9_-]*)$")


def list_existing_subjects(output_root: str | Path) -> list[str]:
    """Return subject labels found under ``output_root/sourcedata/sub-*/``.

    Sorted by mtime descending (most recent first), so the operator
    typically sees the subject they were just running at the top of the
    list. Labels are returned WITHOUT the ``sub-`` prefix.
    """
    sourcedata = Path(output_root) / "sourcedata"
    if not sourcedata.is_dir():
        return []
    entries: list[tuple[float, str]] = []
    for child in sourcedata.iterdir():
        if not child.is_dir():
            continue
        m = _SUB_DIR_RX.match(child.name)
        if m:
            entries.append((child.stat().st_mtime, m.group("label")))
    entries.sort(reverse=True)
    return [label for _, label in entries]


def format_subject_progress(output_root: str | Path, subject: str) -> str:
    """Return a one-line human summary of a subject's current state.

    Examples::

        "discovery: next Level3-1 (cleared so far: 6/22)"
        "practice: 64 / 1100 levels (run 3 / 50)"
        "fresh — no savestate yet"
    """
    sub_dir = Path(output_root) / "sourcedata" / f"sub-{subject}"
    discovery = sub_dir / f"sub-{subject}_phase-discovery_task-mario_savestate.json"
    stable = sub_dir / f"sub-{subject}_phase-stable_task-mario_savestate.json"

    if stable.exists():
        try:
            st = savestate.load(stable)
        except Exception:  # noqa: BLE001 — never crash the GUI from a corrupt file
            return "practice: (savestate unreadable)"
        idx = int(st.get("index", 0))
        # 50 reps × 22 levels = 1100 entries in the canonical design TSV.
        total = 50 * N_LEVELS_PER_RUN
        run_n = idx // N_LEVELS_PER_RUN + 1 if idx < total else 50
        return f"practice: {idx} / {total} levels (run {run_n} / 50)"

    if discovery.exists():
        try:
            st = savestate.load(discovery)
        except Exception:  # noqa: BLE001
            return "discovery: (savestate unreadable)"
        world, level = int(st.get("world", 1)), int(st.get("level", 1))
        # Count how many distinct levels have been cleared so far. This is
        # what `phases.advance_discovery_state` has walked past — same
        # ordering as discovery progression.
        from mario_task.design import ALL_LEVELS  # local import to avoid cycle

        cleared_count = 0
        for w, ell in ALL_LEVELS:
            if (w, ell) == (world, level):
                break
            cleared_count += 1
        else:
            cleared_count = len(ALL_LEVELS)  # discovery done (world≥9)
        return f"discovery: next Level{world}-{level} (cleared: {cleared_count}/{len(ALL_LEVELS)})"

    return "fresh — no savestate yet"


def infer_default_session(output_root: str | Path, subject: str) -> str:
    """Return ``infer_next_session`` result; kept as a thin re-export so the
    GUI module is the single import surface for cli.py."""
    from mario_task.paths import infer_next_session

    return infer_next_session(output_root, subject)


# ---------------------------------------------------------------------------
# Config wizard
# ---------------------------------------------------------------------------


def run_config_wizard(config_path: str | Path) -> Settings | None:
    """Show the first-run wizard. Write ``config.json`` and return Settings on OK.

    Returns ``None`` if the operator cancelled (cli.py exits 0 in that case).
    """
    # Lazy import: tests that don't have a display can import this module
    # but should never call the function.
    from psychopy import gui

    defaults = default_settings()
    backend_choices = [b for b in _VALID_BACKENDS if b != "parallel" or supports_parallel_port()]

    dlg = gui.Dlg(title="mario_task — first-run configuration", labelButtonOK="Save")
    # NB: psychopy.gui.Dlg.show() returns a dict keyed by the FIRST arg of
    # addField (the `key`). We use short keys for dict access below and
    # pass nice human labels via `label=`.
    dlg.addText("EEG / iEEG trigger backend")
    dlg.addField("backend", label="Backend",
                 choices=backend_choices, initial=defaults.triggers.backend,
                 tip="lsl = recommended for iEEG. null = no markers, dev mode.")
    dlg.addField("port", label="Port (serial/parallel only)",
                 initial=defaults.triggers.port or "",
                 tip="Linux: /dev/ttyACM0 or /dev/parport1.  Windows: COM3 etc. "
                     "Ignored when backend is lsl or null.")
    dlg.addField("lsl_stream_name", label="LSL stream name",
                 initial=defaults.triggers.lsl_stream_name,
                 tip="Stream name LabRecorder will see. Only used when backend=lsl.")

    dlg.addText("Task parameters")
    dlg.addField("max_duration", label="Run duration (seconds)",
                 initial=defaults.task.max_duration_seconds,
                 tip="How long each gameplay run lasts.")
    dlg.addField("fixation_duration", label="Fixation duration (seconds)",
                 initial=defaults.task.fixation_duration_seconds)
    dlg.addField("discovery_enabled", label="Run discovery phase",
                 initial=defaults.task.discovery_enabled)
    dlg.addField("practice_enabled", label="Run practice phase",
                 initial=defaults.task.practice_enabled)
    dlg.addField("questionnaire_enabled", label="Show questionnaire after each run",
                 initial=defaults.task.questionnaire_enabled)

    dlg.addText("Enabled levels (uncheck to disable)")
    dlg.addText(
        "  Discovery visits ticked levels in canonical order (1-1, 1-2, ...);"
    )
    dlg.addText(
        "  practice samples them from a depleting pool."
    )
    # Embed a real 8×4 PyQt6 grid widget in the Dlg's layout. psychopy.gui.Dlg
    # is single-column by default, so we drop down a level and add a custom
    # widget that spans both columns of the outer QGridLayout. After OK,
    # grid_widget.get_enabled() returns the ticked levels directly.
    grid_widget = _LevelGridWidget(set(DEFAULT_ENABLED_LEVELS), parent=dlg)
    dlg.layout.addWidget(grid_widget, dlg.irow, 0, 1, 2)
    dlg.irow += 1

    dlg.addText("Display")
    dlg.addField("fullscreen", label="Fullscreen", initial=defaults.display.fullscreen)

    dlg.addText("ROM path")
    dlg.addField("rom_file", label="Mario ROM file", initial=defaults.paths.rom_file,
                 tip="Path to rom.nes. Edit if your ROM lives elsewhere.")
    dlg.addField("data_root", label="Mario data root (states + scenario)",
                 initial=defaults.paths.data_root)

    data = dlg.show()
    if not dlg.OK:
        return None

    enabled_levels = grid_widget.get_enabled()
    if not enabled_levels:
        # Settings._validate would catch this too, but give a friendlier
        # error before save() raises.
        raise ValueError(
            "No levels were enabled in the wizard — at least one level must "
            "be checked. Re-launch and tick at least one box in the level "
            "grid."
        )

    settings = Settings(
        triggers=TriggerSettings(
            backend=data["backend"],
            port=str(data["port"]).strip() or None,
            lsl_stream_name=data["lsl_stream_name"],
        ),
        task=TaskSettings(
            max_duration_seconds=int(data["max_duration"]),
            fixation_duration_seconds=float(data["fixation_duration"]),
            discovery_enabled=bool(data["discovery_enabled"]),
            practice_enabled=bool(data["practice_enabled"]),
            questionnaire_enabled=bool(data["questionnaire_enabled"]),
            enabled_levels=enabled_levels,
        ),
        display=replace(DisplaySettings(), fullscreen=bool(data["fullscreen"])),
        paths=replace(PathSettings(), rom_file=data["rom_file"], data_root=data["data_root"]),
    )
    save(config_path, settings)  # raises on schema violation
    return settings


# Helpers for the level-grid checkboxes. Kept pure so they can be unit-tested.

def _level_field_key(world: int, level: int) -> str:
    return f"level_{world}_{level}"


def _collect_enabled_levels(
    data: dict, possible_levels: tuple[tuple[int, int], ...] = ALL_POSSIBLE_LEVELS
) -> tuple[tuple[int, int], ...]:
    """Build the ``enabled_levels`` tuple from a flat ``{key: bool}`` map.

    Kept for the (unlikely) case where someone reuses the checkbox-dict
    style outside the wizard. The wizard itself uses
    :class:`_LevelGridWidget` and reads directly from the widget.

    Preserves the canonical world/level ordering (1-1, 1-2, ..., 8-4) so
    discovery walks levels in a predictable order regardless of which
    boxes are ticked.
    """
    enabled: list[tuple[int, int]] = []
    for world, level in possible_levels:
        if data.get(_level_field_key(world, level), False):
            enabled.append((world, level))
    return tuple(enabled)


# ---------------------------------------------------------------------------
# 8×4 level-grid widget (PyQt6 — used by the config wizard)
# ---------------------------------------------------------------------------
# Defined inside the gui module but uses a lazy Qt import so the rest of
# the module (and tests) can import it on a headless box where pyqt6 is
# installed but never instantiated.


def _LevelGridWidget(default_enabled, parent=None):
    """Return a QWidget containing an 8×4 checkbox grid for the 32 NES levels.

    Wraps the Qt class definition in a factory function so the heavy
    PyQt6 import only happens when this is actually called (i.e., when
    the wizard opens). Tests that import :mod:`mario_task.gui` without
    a display don't pay the Qt cost.

    The returned widget exposes ``.get_enabled() -> tuple[tuple[int, int], ...]``
    which collects the ticked boxes in canonical (1-1, 1-2, ..., 8-4) order.
    """
    from PyQt6 import QtCore, QtWidgets

    class _Impl(QtWidgets.QWidget):
        def __init__(self, default_enabled: set, parent=None) -> None:
            super().__init__(parent)
            layout = QtWidgets.QGridLayout(self)
            layout.setContentsMargins(8, 4, 8, 4)
            layout.setHorizontalSpacing(28)
            layout.setVerticalSpacing(2)

            # Column headers: blank, "Level 1", ..., "Level 4".
            for col, level in enumerate(range(1, 5), start=1):
                hdr = QtWidgets.QLabel(f"Level {level}")
                hdr.setStyleSheet("font-weight: bold;")
                hdr.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignVCenter)
                layout.addWidget(hdr, 0, col)

            self._checkboxes: dict[tuple[int, int], QtWidgets.QCheckBox] = {}
            for row, world in enumerate(range(1, WORLDS + 1), start=1):
                # Row label.
                lbl = QtWidgets.QLabel(f"World {world}")
                lbl.setStyleSheet("font-weight: bold;")
                layout.addWidget(lbl, row, 0)
                # 4 checkboxes, one per level in this world.
                for col, level in enumerate(range(1, 5), start=1):
                    cb = QtWidgets.QCheckBox()
                    cb.setChecked((world, level) in default_enabled)
                    cb.setToolTip(f"Level {world}-{level}")
                    # Centre the checkbox in its column.
                    container = QtWidgets.QWidget()
                    box = QtWidgets.QHBoxLayout(container)
                    box.setContentsMargins(0, 0, 0, 0)
                    box.addStretch()
                    box.addWidget(cb)
                    box.addStretch()
                    layout.addWidget(container, row, col)
                    self._checkboxes[(world, level)] = cb

        def get_enabled(self) -> tuple[tuple[int, int], ...]:
            """Return ticked levels in canonical (1-1, 1-2, ..., 8-4) order."""
            return tuple(
                (w, l) for (w, l), cb in self._checkboxes.items() if cb.isChecked()
            )

    return _Impl(default_enabled, parent)


# ---------------------------------------------------------------------------
# Subject picker
# ---------------------------------------------------------------------------


def pick_subject(output_root: str | Path) -> tuple[str, str] | None:
    """Show the subject + session picker. Return ``(subject, session)`` or None.

    Pre-populates an info block listing every existing subject with their
    progress, so the operator can read off the screen and type the right
    label. If no subjects exist yet, the block just says "(no existing
    subjects)".
    """
    from psychopy import gui
    from mario_task.paths import normalize_subject

    existing = list_existing_subjects(output_root)

    info_lines: list[str] = []
    if existing:
        info_lines.append("Existing subjects (newest first):")
        for label in existing:
            info_lines.append(
                f"  sub-{label}: {format_subject_progress(output_root, label)}"
            )
    else:
        info_lines.append("(No existing subjects yet — type a new label below.)")

    default_subject = existing[0] if existing else ""
    default_session = (
        infer_default_session(output_root, default_subject) if default_subject else "001"
    )

    dlg = gui.Dlg(title="mario_task — session start", labelButtonOK="Start session")
    dlg.addText("\n".join(info_lines))
    dlg.addField("subject", label="Subject ID", initial=default_subject,
                 tip="Type an existing label to resume, or a new one to start fresh. "
                     "The 'sub-' prefix is optional.")
    dlg.addField("session", label="Session", initial=default_session,
                 tip="3-digit session number. Defaults to the next free one for this subject.")

    data = dlg.show()
    if not dlg.OK:
        return None

    subject = normalize_subject(str(data["subject"]).strip())
    if not subject:
        return None
    session = str(data["session"]).strip() or infer_default_session(output_root, subject)
    return subject, session
